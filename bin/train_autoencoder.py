"""
Script test out the image chunk generation calls.
"""
#pylint: disable=C0413
import sys
import os
import argparse
import functools
#import random
import matplotlib.pyplot as plt
import numpy as np

### Tensorflow includes

import mlflow

import tensorflow as tf
# from tensorflow import keras
#import matplotlib.pyplot as plt

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from delta import config #pylint: disable=C0413
from delta.imagery import imagery_dataset #pylint: disable=C0413
from delta.ml.train import Experiment  #pylint: disable=C0413
from delta.ml.networks import make_autoencoder



# TODO: Move this function!
def get_debug_bands(image_type):
    '''Pick the best bands to use for debug images'''
    bands = [0]
    if image_type == 'worldview':
        # TODO: Distinguish between WV2 and WV3
        bands = [4,2,1] # RGB
    # TODO: Support more sensors
    return bands

# With TF 1.12, the dataset needs to be constructed inside a function passed in to
# the estimator "train_and_evaluate" function to avoid getting a graph error!
def assemble_dataset(config_values):

    # Use wrapper class to create a Tensorflow Dataset object.
    # - The dataset will provide image chunks and corresponding labels.
    ids = imagery_dataset.AutoencoderDataset(config_values)
    ds = ids.dataset()
    ds = ds.repeat(config_values['ml']['num_epochs']).batch(config_values['ml']['batch_size'])
    ds = ds.prefetch(None)

    return ds


def assemble_dataset_for_predict(config_values):
    # Slightly simpler version of the previous function
    ids = imagery_dataset.AutoencoderDataset(config_values)
    ds  = ids.dataset().batch(1) # Batch needed to match the original format
    return ds


def main(argsIn): #pylint: disable=R0914

    usage  = "usage: train_autoencoder [options]"
    parser = argparse.ArgumentParser(usage=usage)

    parser = argparse.ArgumentParser(usage='train_autoencoder.py [options]')

    parser.add_argument("--config-file", dest="config_file", default=None,
                        help="Dataset configuration file.")
    parser.add_argument("--data-folder", dest="data_folder", default=None,
                        help="Specify data folder instead of supplying config file.")
    parser.add_argument("--image-type", dest="image_type", default=None,
                        help="Specify image type along with the data folder."
                        +"(landsat, landsat-simple, worldview, or rgba)")

    parser.add_argument("--num-debug-images", dest="num_debug_images", default=30, type=int,
                        help="Run this many images through the AE after training and write the "
                        "input/output pairs to disk.")

    parser.add_argument("--num-gpus", dest="num_gpus", default=0, type=int,
                        help="Try to use this many GPUs.")

    try:
        options = parser.parse_args(argsIn)
    except argparse.ArgumentError:
        print(usage)
        return -1

    config_values = config.parse_config_file(options.config_file,
                                             options.data_folder, options.image_type)

    #batch_size = config_values['ml']['batch_size']
    #num_epochs = config_values['ml']['num_epochs']


    output_folder = config_values['ml']['output_folder']
    if not os.path.exists(output_folder):
        os.mkdir(output_folder)

    print('loading data from ' + config_values['input_dataset']['data_directory'])
    aeds = imagery_dataset.AutoencoderDataset(config_values)

    # TF additions
    # If the mlfow directory doesn't exist, create it.
    mlflow_tracking_dir = os.path.join(output_folder,'mlruns')
    if not os.path.exists(mlflow_tracking_dir):
        os.mkdir(mlflow_tracking_dir)
    ### end if

    print('Creating experiment')
    experiment = Experiment(mlflow_tracking_dir,
                            'autoencoder_relu_out_%s'%(config_values['input_dataset']['image_type'],),
                            output_dir=output_folder)
    mlflow.log_param('image type',   config_values['input_dataset']['image_type'])
    mlflow.log_param('image folder', config_values['input_dataset']['data_directory'])
    mlflow.log_param('chunk size',   config_values['ml']['chunk_size'])
    print('Creating model')
    data_shape = (aeds.chunk_size(), aeds.chunk_size(), aeds.num_bands())
    model = make_autoencoder(data_shape, encoding_size=config_values['ml']['num_hidden'])
    print('Training')

    # Estimator interface requires the dataset to be constructed within a function.
    tf.logging.set_verbosity(tf.logging.INFO)
    dataset_fn = functools.partial(assemble_dataset, config_values)
    estimator = experiment.train(model, dataset_fn, steps_per_epoch=1000,
                                 log_model=False, num_gpus=options.num_gpus)
    #model = experiment.train_keras(model, dataset_fn,
    #                               num_epochs=config_values['ml']['num_epochs'],
    #                               steps_per_epoch=150,
    #                               log_model=False, num_gpus=options.num_gpus)

    print('Saving Model')
    if config_values['ml']['model_dest_name'] is not None:
        out_filename = os.path.join(output_folder, config_values['ml']['model_dest_name'])
        tf.keras.models.save_model(model, out_filename, overwrite=True, include_optimizer=True)
        mlflow.log_artifact(out_filename)
    ### end if


    # Write input/output image pairs to the current working folder.
    print('Recording ', str(options.num_debug_images), ' demo images.')


    # Make a non-shuffled dataset with a simple iterator
    ds = assemble_dataset_for_predict(config_values)
    iterator = ds.make_one_shot_iterator()
    next_element = iterator.get_next()
    sess = tf.Session()

    scale = aeds.scale_factor()
    num_bands = data_shape[0]
#     debug_bands = get_debug_bands(config_values['input_dataset']['image_type'])
    for i in range(0, options.num_debug_images):
        print('Preparing debug image ' + str(i))
        # Get the next image pair, then make a function to return it
        value = sess.run(next_element)
        def temp_fn():
            return value #pylint: disable=W0640

        # Get a generator from the predictor and get the only value from it
        result = estimator.predict(temp_fn)
        element = next(result)

        # Get the output value out of its weird format, then convert for image output
#         pic = (element['reshape'][debug_bands, :, :] * scale).astype(np.uint8)
        pic = (element['reshape'][:, :, :] * scale).astype(np.uint8)
        pic = np.moveaxis(pic, 0, -1) #TODO: Do we still need this with Brian's channel change?

        in_pic = (value[0][0,:,:,:] * scale).astype(np.uint8)
        in_pic = np.moveaxis(in_pic, 0, -1) #TODO: Do we still need this with Brian's channel change?

        for band in range(num_bands):
            plt.subplot(num_bands,2,2*band+1)
            plt.imshow(in_pic[:,:,band])
            if band == 0:
                plt.title('Input image %03d' % (i, ))

            plt.subplot(num_bands,2,2*band+2)
            plt.imshow(pic[:,:,band])
            if band == 0:
                plt.title('Output image %03d' % (i, ))

        debug_image_filename = os.path.join(output_folder,
                                            'Autoencoder_input_output_%03d.png' % (i, ))
        plt.savefig(debug_image_filename)

        mlflow.log_artifact(debug_image_filename)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
