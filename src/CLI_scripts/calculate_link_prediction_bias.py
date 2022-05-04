# This script is based on: https://github.com/mianzg/kgbiasdetec
# More specifically, the following files:
#   experiments/run_tail_prediction.py
#   predict_tails.py

### Imports

# in-built modules
import argparse
import csv
import logging
import os
import random
import shutil
import socket
import sys
import time
from datetime import datetime
import copy
import pickle

# imports from installed libraries
from tqdm import tqdm, trange
import numpy as np
import pandas as pd
import torch

# imports from my own code
from src.utils import set_base_path_based_on_host, initialize_my_logger, \
    improve_pandas_viewing_options, save_argparse_obj_to_disk
from src.utils_with_pykeen import HumanWikidata5M_pykeen
from src.bias_measurement.link_prediction_bias.predict_tails import get_preds_df
from src.bias_measurement.link_prediction_bias.BiasEvaluator import BiasEvaluator
from src.bias_measurement.link_prediction_bias.Measurement import DemographicParity, PredictiveParity

BASE_PATH_HOST = set_base_path_based_on_host()
improve_pandas_viewing_options()


def get_sensitive_and_target_relations(dataset_name):
    if dataset_name.lower() == "wikidata5m":
        target_relations = ['P106']  # occupation
        sensitive_relations = ['P21']  # sex or gender
        # The k most common relations in Wikidata5M (old version)
        # P27 - country of citizenship                  #######################################
        # P54 - member of sports team                   #######################################
        # P735 - given name                             #######################################
        # P19 - place of birth                          #######################################
        # P69 - educated at                             #######################################
        # P641 -  sport                                   #######################################
        # P20 - place of death                          #######################################
        # P1412 - languages spoken, written or signed   #######################################
        # P1344 - participant in                        #######################################
        # P413 - position played on team/specialty      #######################################
        # P166 - award received                         #######################################
    else:
        raise NotImplementedError('Other datasets than Wikidata5M are currently not implemented.')

    # This is required such that the relations can be looped through meaningfully
    # relevant for predict_tails.add_relation_values(), Measurement.py
    assert type(sensitive_relations) == list
    assert type(target_relations) == list

    all([type(item) == str for item in sensitive_relations])
    all([type(item) == str for item in target_relations])

    return sensitive_relations, target_relations


def create_preds_df():
    """
    1. This uses the predict_tails.get_preds_df() function from Keidar.
    2. This function then calls utils.get_classifier().
    3. classifier.train() then trains an occupation classifier based on the embeddings
       of the link prediction model.
    4. predict_tails.predict_relation_tails() creates the first 4 columns of the preds_df:
       entity, relation, true tail, preds (by the classifier)
       This shows true labels for occupation and what the classifier predicted.
    5. predict_tails.add_relation_values() gets the sensitive attribute values for each person
       in preds_df from the original dataset, e.g. who is female/male/value missing.
    6. This dataframe is then stored as CSV, one preds_df per model.

    Arguments needed for this are:
        dataset = a dataset object, originally inherited from pykeen.PathDataset(LazyDataset(Dataset))
        classifier_args = epochs, batch size, type (e.g. MLP), number of classes
        model_args = the path to a trained link prediction model
        target_relation = an identifier of the target relation, will be 'P106' (occupation) for W5M
        bias_relations = list of identifier for relations that bias should be calculated for
        (originally in utils.suggest_relations: ['P27', 'P735', 'P19', 'P54', 'P69', 'P641', 'P20', 'P1344', 'P1412', 'P413'])


    Returns
    -------

    """

    pass


START_TIME = datetime.now().strftime("%d.%m.%Y_%H:%M")

parser = argparse.ArgumentParser(
    description = 'Calculate bias on Wikidata5M using a trained link prediction model')

parser.add_argument('--model_type', required = True, choices = {'KG_only', 'KG+LM', 'LM_only'},
                    help = 'Specify the model type that is used.')
parser.add_argument('-n', '--name', required = True, type = str,
                    help = 'Experiment name. A folder with this name will be created  or loaded '
                           'from in the results/bias_measurement/link_prediction_bias folder.')
parser.add_argument("--data_dir", default = 'data/processed/human_Wikidata5M', type = str,
                    help = "The input data dir. Should contain the .tsv files (or other data files) for the task.")

# parameters for training a classifier
parser.add_argument('--epochs', type = int,
                    help = "Number of training epochs of link prediction classifier (used for DP & PP), default to 100",
                    default = 10)
parser.add_argument('--batch', type = int,
                    help = "Training batch size for the target relation classifier.", default = 256)
parser.add_argument('--clsf_type', type = str, choices = {'mlp', 'rf'},
                    help = "Type of target relation classifier.", default = 'mlp')
parser.add_argument('--num_classes', type = int,
                    help = "Number of training epochs of link prediction classifier (used for DP & PP), default to 100",
                    default = 11)

# paramters needed when classifier/preds_df is loaded from existing folder
parser.add_argument('--load_from_folder', action = 'store_true',
                    help = 'Add this flag when you want to load existing files, e.g. a trained '
                           'classifier and/or a preds_df. Files will be loaded from an existing '
                           'folder specified with the "name" parameter.')
parser.add_argument('--trained_classifier', type = str,
                    help = 'File name of the checkpoint file to load from.')

# parameters needed in both cases
parser.add_argument('--embedding_name', type = str, default = 'transe',
                    choices = {'transe', 'complex', 'distmult', 'quate', 'rotate', 'simple'}, help = "Only valid for KG_only. Embedding name, must be one of complex, conv_e, distmult, rotate, trans_d, trans_e. \
                            Defaults to transe")
parser.add_argument('--embedding_path', type = str, help = "Specify a full path to your trained embedding model. It will override default path \
                          inferred by dataset and embedding")
parser.add_argument('--predictions_path', type = str, help = 'path to predictions used in parity distance, specifying \
                           it will override internal inferred path')

parser.add_argument('--use-pretrained', action = 'store_true', help = "")
parser.add_argument('--dataset_name', type = str, default = 'Wikidata5M',
                    choices = {'Wikidata5M', 'FB15K-237'},
                    help = "Dataset name, must be one of Wikidata5M or FB15K-237. Defaults to Wikidata5M.")
parser.add_argument('-d', "--debug", action = 'store_true', help = "Add this flag when debugging.")
parser.add_argument("--random_seed", default = 42, type = int,
                    help = "Integer specifying a random seed")

args = parser.parse_args()

dict_of_necessary_files = {'train_filename': 'train.tsv', 'validation_filename': 'validation.tsv',
                           'test_filename': 'test.tsv'}

# IMPORTANT: execute this code when training, e.g. save folder
if not args.load_from_folder:
    if args.debug:
        EXPERIMENT_NAME = 'DEBUGGING_' + START_TIME + '_' + args.name
        # test has 10 examples, training has
        args.data_dir = os.path.join(args.data_dir, 'for_debugging')
        args.num_classes = 2
    else:
        EXPERIMENT_NAME = START_TIME + '_' + args.name

    # IMPORTANT Check that the data_dir exists and that it contains the necessary files
    if not os.path.exists(os.path.join(BASE_PATH_HOST, args.data_dir)):
        raise FileNotFoundError('The data directory does not exist!')
    for file_name in dict_of_necessary_files.values():
        assert os.path.isfile(os.path.join(BASE_PATH_HOST, args.data_dir,
                                           file_name)), f'File {file_name} does not exist in the specified data_dir!'

    DIRECTORY_FOR_SAVING_OR_LOADING = os.path.join(BASE_PATH_HOST,
                                                   'results/bias_measurement/link_prediction_bias',
                                                   EXPERIMENT_NAME)

    # create directory and then use it as working directory
    # if os.path.exists(DIRECTORY_FOR_SAVING_OR_LOADING) and os.listdir(
    #        DIRECTORY_FOR_SAVING_OR_LOADING) and args.do_train:
    #    raise ValueError(f"Output directory ({DIRECTORY_FOR_SAVING_OR_LOADING}) already exists and is not empty.")
    if not os.path.exists(DIRECTORY_FOR_SAVING_OR_LOADING):
        os.makedirs(DIRECTORY_FOR_SAVING_OR_LOADING)

        # change working directory to respective folder
        os.chdir(DIRECTORY_FOR_SAVING_OR_LOADING)

    # save the currently running script file for later reference
    file_name_script = 'script_' + EXPERIMENT_NAME + '.py'
    source_path = os.path.join(BASE_PATH_HOST, 'src/CLI_scripts', __file__)
    shutil.copy(src = source_path, dst = os.path.join(os.getcwd(), file_name_script))

    # save the argparse arguments to disk
    save_argparse_obj_to_disk(argparse_namespace = args)

    # initialize logger
    logger_file_name = f'log_train_LP_bias_{socket.gethostname()}_' + EXPERIMENT_NAME + '.txt'
    logger = initialize_my_logger(file_name = logger_file_name, level = logging.DEBUG)
    logger.info(f'Saving everything in folder: {DIRECTORY_FOR_SAVING_OR_LOADING}')
    logger.info(f'Loading data from: {args.data_dir}')

    if args.debug:
        logger.info(f'DEBUGGING MODE: Using a very small subset of {args.dataset_name}.')

# IMPORTANT do this when loading files from an existing results folder
else:
    # if not training, save everything to the folder where the model is loaded from
    EXPERIMENT_NAME = args.name
    DIRECTORY_FOR_SAVING_OR_LOADING = os.path.join(BASE_PATH_HOST,
                                                   'results/bias_measurement/link_prediction_bias',
                                                   EXPERIMENT_NAME)
    # IMPORTANT Check that the data_dir exists and that it contains the necessary files
    if not os.path.exists(DIRECTORY_FOR_SAVING_OR_LOADING):
        raise FileNotFoundError(
            'Directory does not exist! Experiment name must refer to an existing '
            'directory inside results/bias_measurement/link_prediction_bias!')
    # Check that the data_dir exists and that it contains the necessary files
    if not os.path.exists(os.path.join(BASE_PATH_HOST, args.data_dir)):
        raise FileNotFoundError('The data directory does not exist!')
    # check that the given trained model file exists
    if args.trained_classifier is not None:
        assert os.path.isfile(os.path.join(DIRECTORY_FOR_SAVING_OR_LOADING, args.trained_classifier)),\
            f'The given trained classifier file does not exist in the specified folder!'

    # change working directory to respective folder
    os.chdir(DIRECTORY_FOR_SAVING_OR_LOADING)

    # initialize logger
    logger_file_name = f'log_evaluate_LP_bias_{socket.gethostname()}_' + START_TIME + '.txt'
    logger = initialize_my_logger(file_name = logger_file_name, level = logging.DEBUG)
    logger.info(f'Saving everything in folder: {DIRECTORY_FOR_SAVING_OR_LOADING}')
    logger.info(f'Loading data from: {args.data_dir}')

logger.info('################# DEVICE INFORMATION #################')
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
logger.info(f'Device used: {device}')
logger.info(f'CUDA is used: {torch.cuda.is_available()}')
logger.info(f'Using {torch.cuda.device_count()} device(s).')

# set the random seeds
random.seed(args.random_seed)
np.random.seed(args.random_seed)
torch.manual_seed(args.random_seed)

### Create variables needed by Keidar code

dataset = HumanWikidata5M_pykeen(base_path_host = BASE_PATH_HOST,
                                 rel_training_set_path = os.path.join(args.data_dir,
                                                                      dict_of_necessary_files.get(
                                                                          'train_filename')),
                                 rel_validation_set_path = os.path.join(args.data_dir,
                                                                        dict_of_necessary_files.get(
                                                                            'validation_filename')),
                                 rel_test_set_path = os.path.join(args.data_dir,
                                                                  dict_of_necessary_files.get(
                                                                      'test_filename')))

sensitive_relations, target_relations = get_sensitive_and_target_relations(args.dataset_name)

PATH_TO_EMBEDDING = os.path.join(BASE_PATH_HOST,
                                 f'trained_models/KG_only/graphvite_pretrained_W5M/{args.embedding_name}_pretrained_human_W5M.pkl')
PATH_TO_EMBEDDING = os.path.join(BASE_PATH_HOST,
                                 'results/KG_only/TransE_fullW5M_80epochs/trained_model.pkl')
PATH_TO_EMBEDDING = os.path.join(BASE_PATH_HOST,
                                 f'trained_models/KG_only/graphvite_pretrained_W5M/{args.embedding_name}_pretrained_human_W5M_entity_embeddings_dict.pkl')


# Check whether a preds_df already exists in the current folder, if yes it is loaded
list_of_preds_df_files = []
for file in os.listdir(os.getcwd()):
    if file.startswith('preds_df'):
        list_of_preds_df_files.append(file)
if not list_of_preds_df_files:
    # preds_df_path should be None if classifier should be trained
    logger.info('No preds_df exists yet, training or loading a target relation classifier.')
    path_to_existing_preds_df = None
elif len(list_of_preds_df_files) == 1:
    path_to_existing_preds_df = list_of_preds_df_files[0]
    logger.info(f'Loading existing preds_df: {list_of_preds_df_files[0]}')
else:
    raise ValueError('More than one preds_df found, but only one expected!')

# Create arguments needed for training the target relation classifier
model_args = {'embedding_model_path': PATH_TO_EMBEDDING}

classifier_args = {'epochs': args.epochs, "batch_size": args.batch, "type": args.clsf_type,
                   'num_classes': args.num_classes,
                   'load_trained_classifier': args.load_from_folder,
                   'trained_classifier_filename': args.trained_classifier}

preds_df = get_preds_df(dataset = dataset, classifier_args = classifier_args,
                        model_args = model_args, target_relation = target_relations,
                        bias_relations = sensitive_relations,
                        preds_df_path = path_to_existing_preds_df)

# save preds_df to disk in case it was just created
if path_to_existing_preds_df is None:
    preds_df.to_csv(f'preds_df_{args.model_type}_{args.embedding_name}_{EXPERIMENT_NAME}.csv')

# Given a preds_df, calculate bias scores for given mesaures

assert preds_df.empty is False
measures = [DemographicParity(), PredictiveParity()]
evaluator = BiasEvaluator(dataset, measures)
evaluator.set_predictions_df(preds_df)
bias_eval = evaluator.evaluate_bias(bias_relations = sensitive_relations, bias_measures = measures)
d_parity, p_parity = bias_eval['demographic_parity'], bias_eval['predictive_parity']

from src.bias_measurement.link_prediction_bias.utils import save_result
# to Save dataset summary, and output from Evaluator

save_result(result = bias_eval,
            dataset = dataset,
            args = args)

logger.info(f'Finished running the script at: {datetime.now().strftime("%d.%m.%Y %H:%M")}')