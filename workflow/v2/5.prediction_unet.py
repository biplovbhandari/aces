# -*- coding: utf-8 -*-

try:
    from aces.config import Config
except ModuleNotFoundError:
    print("ModuleNotFoundError: Attempting to import from parent directory.")
    import os, sys
    sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

    from aces.config import Config

import json
import os
import tensorflow as tf
import numpy as np
import subprocess


OUTPUT_IMAGE_FILE = str(Config.MODEL_DIR / "prediction" / f"{Config.OUTPUT_NAME}.TFRecord")
if not os.path.exists(str(Config.MODEL_DIR / "prediction")): os.mkdir(str(Config.MODEL_DIR / "prediction"))
print(f"OUTPUT_IMAGE_FILE: {OUTPUT_IMAGE_FILE}")

OUTPUT_GCS_PATH = f"gs://{Config.GCS_BUCKET}/prediction/{Config.OUTPUT_NAME}.TFRecord"
print(f"OUTPUT_GCS_PATH: {OUTPUT_GCS_PATH}")

ls = f"sudo gsutil ls gs://{Config.GCS_BUCKET}/{Config.GCS_IMAGE_DIR}/"
print(f"ls >> : {ls}")
files_list = subprocess.check_output(ls, shell=True)
files_list = files_list.decode("utf-8")
files_list = files_list.split("\n")

# Get only the files generated by the image export.
exported_files_list = [s for s in files_list if Config.GCS_IMAGE_PREFIX in s]

print(f"exported_files_list: {exported_files_list}")

# Get the list of image files and the JSON mixer file.
image_files_list = []
json_file = None
for f in exported_files_list:
    if f.endswith(".tfrecord.gz"):
        image_files_list.append(f)
    elif f.endswith(".json"):
        json_file = f

# Make sure the files are in the right order.
image_files_list.sort()

print(f"image_files_list: {image_files_list}")

print(f"json_file: {json_file}")

if Config.USE_BEST_MODEL_FOR_INFERENCE:
    print(f"Using best model for inference.\nLoading model from {str(Config.MODEL_DIR)}/{Config.MODEL_CHECKPOINT_NAME}.tf")
    this_model = tf.keras.models.load_model(f"{str(Config.MODEL_DIR)}/{Config.MODEL_CHECKPOINT_NAME}.tf")
else:
    print(f"Using last model for inference.\nLoading model from {str(Config.MODEL_DIR)}/trained-model")
    this_model = tf.keras.models.load_model(f"{str(Config.MODEL_DIR)}/trained-model")

print(this_model.summary())


cat = f"gsutil cat {json_file}"
read_t = subprocess.check_output(cat, shell=True)
read_t = read_t.decode("utf-8")

# Get a single string w/ newlines from the IPython.utils.text.SList
mixer = json.loads(read_t)

# Get relevant info from the JSON mixer file.
patch_width = mixer["patchDimensions"][0]
patch_height = mixer["patchDimensions"][1]
patches = mixer["totalPatches"]
patch_dimensions_flat = [patch_width * patch_height, 1]

# Get set up for prediction.
if Config.KERNEL_BUFFER:
    x_buffer = Config.KERNEL_BUFFER[0] // 2
    y_buffer = Config.KERNEL_BUFFER[1] // 2

    buffered_shape = [
        Config.PATCH_SHAPE[0] + Config.KERNEL_BUFFER[0],
        Config.PATCH_SHAPE[1] + Config.KERNEL_BUFFER[1],
    ]
else:
    x_buffer = 0
    y_buffer = 0
    buffered_shape = Config.PATCH_SHAPE

if Config.USE_ELEVATION:
    Config.FEATURES.extend(["elevation", "slope"])


if Config.USE_S1:
    Config.FEATURES.extend(["vv_asc_before", "vh_asc_before", "vv_asc_during", "vh_asc_during",
                            "vv_desc_before", "vh_desc_before", "vv_desc_during", "vh_desc_during"])

print(f"Config.FEATURES: {Config.FEATURES}")

image_columns = [
    tf.io.FixedLenFeature(shape=buffered_shape, dtype=tf.float32) for k in Config.FEATURES
]

image_features_dict = dict(zip(Config.FEATURES, image_columns))

def parse_image(example_proto):
    return tf.io.parse_single_example(example_proto, image_features_dict)

def toTupleImage(inputs):
    inputsList = [inputs.get(key) for key in Config.FEATURES]
    stacked = tf.stack(inputsList, axis=0)
    stacked = tf.transpose(stacked, [1, 2, 0])
    return stacked

# Create a dataset from the TFRecord file(s) in Cloud Storage.
image_dataset = tf.data.TFRecordDataset(image_files_list, compression_type="GZIP")
image_dataset = image_dataset.map(parse_image, num_parallel_calls=5)
image_dataset = image_dataset.map(toTupleImage).batch(1)

# for inputs in image_dataset.take(1):
#     print("inputs", inputs)
#     print(f"inputs: {inputs.shape}")

# Perform inference.
print("Running predictions...")
print(f"patches: {patches}")
predictions = this_model.predict(image_dataset, steps=patches, verbose=1)
print(f"predictions: {predictions.shape}")

print("Writing predictions...")
writer = tf.io.TFRecordWriter(OUTPUT_IMAGE_FILE)

for i, prediction_patch in enumerate(predictions):
    if i == 0:
        print(f"Starting with patch {i}...")
        print(f"predictionPatch: {prediction_patch.shape}")

    if i % 50 == 0:
        print(f"Writing patch {i}...")

    prediction_patch = prediction_patch[
        x_buffer: x_buffer+Config.PATCH_SHAPE[0],
        y_buffer: y_buffer+Config.PATCH_SHAPE[1]
    ]

    example = tf.train.Example(
        features=tf.train.Features(
            feature={
            "prediction": tf.train.Feature(
                int64_list=tf.train.Int64List(
                    value=np.argmax(prediction_patch, axis=-1).flatten())),
            "cropland_etc": tf.train.Feature(
                float_list=tf.train.FloatList(
                    value=prediction_patch[:, :, 0:1].flatten())),
            "rice": tf.train.Feature(
                float_list=tf.train.FloatList(
                    value=prediction_patch[:, :, 1:2].flatten())),
            "forest": tf.train.Feature(
                float_list=tf.train.FloatList(
                    value=prediction_patch[:, :, 2:3].flatten())),
            "urban": tf.train.Feature(
                float_list=tf.train.FloatList(
                    value=prediction_patch[:, :, 3:4].flatten())),
            "others_etc": tf.train.Feature(
                float_list=tf.train.FloatList(
                    value=prediction_patch[:, :, 4:5].flatten())),
            }
        )
    )

    i += 1

    # Write the example.
    writer.write(example.SerializeToString())

writer.close()

# upload to gcp
upload_to_gcp = f"sudo gsutil cp {OUTPUT_IMAGE_FILE} {OUTPUT_GCS_PATH}"
result = subprocess.check_output(upload_to_gcp, shell=True)
print(f"uploading classified image to earth engine: {result}")

# upload to earth engine asset
upload_image = f"earthengine upload image --asset_id={Config.EE_OUTPUT_ASSET}/{Config.OUTPUT_NAME} --pyramiding_policy=mode {OUTPUT_GCS_PATH} {json_file}"
result = subprocess.check_output(upload_image, shell=True)
print(f"uploading classified image to earth engine: {result}")
