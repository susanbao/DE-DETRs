import torch
import numpy as np
import os
import json

def np_read_with_tensor_output(file):
    with open(file, "rb") as outfile:
        data = np.load(outfile)
    return torch.from_numpy(data.astype(np.float32))

def read_json_results(path):
    with open(path, "r") as outfile:
        data = json.load(outfile)
    return data

def write_json_results(json_data, path):
    with open(path, "w") as outfile:
        json.dump(json_data, outfile)

def np_read(file):
    with open(file, "rb") as outfile:
        data = np.load(outfile)
    return data
def np_write(data, file):
    with open(file, "wb") as outfile:
        np.save(outfile, data)

def create_folder_if_not_exists(folder_path):
    if not os.path.exists(folder_path):
        os.makedirs(folder_path)