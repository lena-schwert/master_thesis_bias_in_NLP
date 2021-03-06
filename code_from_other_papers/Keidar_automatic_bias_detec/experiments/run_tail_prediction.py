""""
Code for getting a tail predictions dataframe
"""
import argparse
import os
import numpy as np
from sklearn.metrics import balanced_accuracy_score, accuracy_score
from pykeen.datasets import FB15k237

### Internal Imports
from code_from_other_papers.Keidar_automatic_bias_detec.utils import suggest_relations
from code_from_other_papers.Keidar_automatic_bias_detec.predict_tails import get_preds_df
from code_from_other_papers.Keidar_automatic_bias_detec.visualization import preds_histogram

def set_argparser():
    # Parser
    parser = argparse.ArgumentParser()
    parser.add_argument('--dataset', type=str, default='fb15k237',
                        help="Dataset name, must be one of fb15k237. Default to fb15k237.")
    parser.add_argument('--embedding', type=str, default='trans_e',
                        help="Embedding name, must be one of complex, conv_e, distmult, rotate, trans_d, trans_e. \
                                Default to trans_e")
    parser.add_argument('--embedding_path', type=str,
                        help="Specify a full path to your trained embedding model. It will override default path \
                              inferred by dataset and embedding")
    parser.add_argument('--predictions_path', type=str,
                        help='path to predictions used in parity distance, specifying \
                               it will override internal inferred path')
    # TODO change default back to 100
    parser.add_argument('--epochs', type=int,
                        help="Number of training epochs of link prediction classifier (used for DP & PP), default to 100",
                        default=100)
    parser.add_argument('--batch', type=int,
                        help="Number of training epochs of link prediction classifier (used for DP & PP), default to 100",
                        default=256)
    parser.add_argument('--clsf_type', type=str,
                        help="Number of training epochs of link prediction classifier (used for DP & PP), default to 100",
                        default='mlp')
    parser.add_argument('--num_classes', type=int,
                        help="Number of training epochs of link prediction classifier (used for DP & PP), default to 100",
                        default=6)
    args = parser.parse_args()
    return args

args = set_argparser()

import socket
if socket.gethostname() == 'Schlepptop':
    base_dir = '/home/lena/git/master_thesis_bias_in_NLP/'
# covers all CPU + GPU nodes of the HPI
elif 'node' in socket.gethostname() or socket.gethostname() in ['a6k5-01', 'dgxa100-01', 'ac922-01',
                                                                'ac922-02']:
    base_dir = '/hpi/fs00/scratch/lena.schwertmann/pycharm_master_thesis'
else:
    base_dir = None
    ValueError("Host name is not recognized!")
assert type(base_dir) == str, "Path has not been set correctly as string, doublecheck!"

# Init dataset and relations of interest
dataset = FB15k237()   # built-in from pykeen

# simply accesses
target_relation, bias_relations = suggest_relations(args.dataset)

# Set your local path here
LOCAL_PATH_TO_EMBEDDING = '/Users/alacrity/Documents/uni/Fairness/'

# Trained Embedding Model Path
# embedding_model_path_suffix = "replicates/replicate-00000/trained_model.pkl"
# MODEL_PATH = os.path.join(LOCAL_PATH_TO_EMBEDDING, args.dataset, args.embedding, embedding_model_path_suffix)
# if args.embedding_path:
#     MODEL_PATH = args.embedding_path  # override default if specifying a full path
# print("Load embedding model from: {}".format(MODEL_PATH))

#TODO change it back
MODEL_PATH = os.path.join(base_dir, 'results/KG_only/TransE_fullW5M_80epochs/trained_model.pkl')

PREDS_DF_PATH = '/home/lena/git/master_thesis_bias_in_NLP/code_from_other_papers/Keidar_automatic_bias_detec/preds_dfs/preds_df_transe.csv'

# Init embed model and classifier parameter
model_args = {'embedding_model_path': MODEL_PATH}

classifier_args = {'epochs': args.epochs,
                   "batch_size": args.batch,
                   "type": args.clsf_type,
                   'num_classes': args.num_classes}

# You can set a path to save the predictions dataframe
SAVE_PATH = 'preds_df_transe_created_by_lena.csv'

# IMPORTANT if you don't want to train a classifier, provide a path to a preds_df
PATH_TO_PREDS_DF = '/home/lena/git/master_thesis_bias_in_NLP/code_from_other_papers/Keidar_automatic_bias_detec/preds_dfs/preds_df_transe.csv'

preds_df = get_preds_df(dataset,
                        classifier_args,
                        model_args,
                        target_relation,
                        bias_relations,
                        # IMPORTANT path should be None if classifier should be trained
                        preds_df_path=None
                        )

# Evaluate the predictions
random_preds = [np.random.randint(args.num_classes) for __ in preds_df.pred]
print("classification accuracy for random labels", accuracy_score(preds_df.true_tail, random_preds))
print("balanced classification accuracy for random labels", balanced_accuracy_score(preds_df.true_tail, random_preds))

print("classification accuracy for rf model", accuracy_score(preds_df.true_tail, preds_df.pred))
print("balanced classification accuracy for rf model", balanced_accuracy_score(preds_df.true_tail, preds_df.pred))
preds_histogram(preds_df)