#!/usr/bin/env python
"""
Generate simulated strong lensing images using the classes and parameters of
an input configuration dictionary.

This script generates strong lensing images from paltas config dictionaries.

Example
-------
To run this script, pass in the desired config as argument::

    $ python -m generate.py path/to/config.py path/to/save_folder --n 1000

The parameters will be pulled from config.py and the images will be saved in
save_folder. If save_folder doesn't exist it will be created.
"""
import numpy as np
import os
import shutil
import matplotlib.pyplot as plt
from tqdm import tqdm
import pandas as pd
from paltas.core import Paltas
from paltas.Utils.cli_maker import make_cli


def generate_from_config(
    config_path,
    save_folder,
    n: int = 1,
    save_png_too: bool = False,
    tf_record: bool = False,
):
    """Generate simulated strong lensing images

    Args:
        config_path: Path to paltas configuration file
        save_folder: Folder to save images to
        n: Size of dataset to generate (default 1)
        save_png_too: if True, also save a PNG for each image for debugging
        tf_record: if True, generate the tfrecord for the dataset
    """
    # Make the directory if not already there
    if not os.path.exists(save_folder):
        os.makedirs(save_folder)
    print("Save folder path: {:s}".format(save_folder))

    # Copy out config dict
    shutil.copy(os.path.abspath(config_path), save_folder)

    # Gather metadata in a list, will be written to dataframe later
    metadata_list = []
    metadata_path = os.path.join(save_folder, "metadata.csv")

    # Initialize our config handler
    config_handler = Paltas(config_path)

    # Generate our images
    pbar = tqdm(total=n)
    successes = 0
    tries = 0
    while successes < n:
        # We always try
        tries += 1

        # Attempt to draw our image
        image, metadata = config_handler.draw_image(new_sample=True)

        # Failed attempt if there is no image output
        if image is None:
            continue

        # Save the image and the metadata
        filename = os.path.join(save_folder, "image_%07d" % successes)
        np.save(filename, image)
        if save_png_too:
            plt.imsave(
                filename + ".png",
                np.log10(image.clip(0, None)),
                cmap=plt.cm.magma,
            )

        metadata_list.append(metadata)

        # Write out the metadata every 20 images, and on the final write
        if len(metadata_list) > 20 or successes == n - 1:
            df = pd.DataFrame(metadata_list)
            # Sort the keys lexographically to ensure consistent writes
            df = df.reindex(sorted(df.columns), axis=1)
            first_write = successes <= len(metadata_list)
            df.to_csv(
                metadata_path,
                index=None,
                mode="w" if first_write else "a",
                header=first_write,
            )
            metadata_list = []

        successes += 1
        pbar.update()

    # Make sure the list has been cleared out.
    assert not metadata_list
    pbar.close()
    print("Dataset generation complete. Acceptance rate: %.3f" % (n / tries))

    # Generate tf record if requested. Save all the parameters and use default
    # filename data.tfrecord
    if tf_record:
        print("paltas.Analysis has been removed, get your tfrecords elsewhere")


if __name__ == "__main__":
    make_cli(generate_from_config)
