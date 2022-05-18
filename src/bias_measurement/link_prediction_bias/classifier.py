"""
Wrappers for MLP and random forest classifier, on the task of profession prediction
"""
# Built-in modules
import os
import pprint
import operator
from datetime import datetime
import logging
import pickle

# Installed modules
import numpy as np
import pandas as pd
from collections import Counter
from pykeen.datasets import FB15k237
# Ignite
from ignite.contrib.handlers import ProgressBar
# from ignite.contrib.metrics import ROC_AUC
from ignite.engine import Engine, Events
from ignite.metrics import Accuracy, Precision, Recall, RunningAverage
from ignite.handlers import Checkpoint, ModelCheckpoint
from ignite.contrib.handlers.tensorboard_logger import *
# Torch
from torch.utils.data import DataLoader
import torch
import torch.nn as nn

#### Internal Imports
from src.bias_measurement.link_prediction_bias.classifier_models import MLP

# makes it compatible with logging coming from other sources
logger = logging.getLogger(__name__)


class TargetRelationClassifier:

    def __init__(self, dataset, embedding_model_path, target_relation, num_classes,
                 trained_classifier_filename: str, load_trained_classifier = False,
                 batch_size = 200, lr = 0.01, model_type = 'mlp', **model_kwargs, ):
        """"
        embedding_model_path : path to the kg embedding that will be used
        num_classes : number of labels to consider. The classifier will learn
            to predict the (num_classes -1) most frequent labels,
            and consider all the rest to be of class OTHER
        hidden_layer_sizes
        batch_size : the batch size used when training
        """
        # IMPORTANT make this class label obviously different than the others!
        self.OTHER = -1

        self._device = self.DEVICE = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self._dataset = dataset
        self.num_classes = num_classes
        self._batch_size = batch_size
        self._target = target_relation

        # retrieve train + test triple factories and DataLoaders
        # creates: self.train_triples_factory, self._train_loader, self.test_triples_factory, self._test_loader
        self.set_data_loaders(target_relation = target_relation)

        # retrieve (num_classes -1) most frequent tail values (after target relation)
        # and map them + self.OTHER to numeric class labels
        # creates: self._target2int
        self.set_target_labels()

        self.binary = (num_classes == 2)
        self.set_loss(binary = self.binary)

        # load trained (pykeen) model from path/pre-trained Graphvite embeddings
        # will be used by: train(), predict_tails()
        # IMPORTANT If using graphvite pretrained embeddings, do additional pre-processing
        # First iteration: use an EasyDict
        # needed properties: model.embedding_dim, model.entity_embeddings(numeric IDs)+

        # convert QIDs to dataset-specific numeric IDs
        with open(embedding_model_path, 'rb') as f:
            graphvite_human_entity_embeddings = pickle.load(f)
        # convert original numpy arrays to torch tensors
        HumanWikidata5M_ID_to_embedding = {dataset.entity_to_id.get(key): torch.from_numpy(value)
                                           for key, value in
                                           graphvite_human_entity_embeddings.items()}

        model = {'embedding_dim': list(graphvite_human_entity_embeddings.values())[0].shape[0],
                 # dim = 512
                 'entity_embeddings_dict': HumanWikidata5M_ID_to_embedding}

        # How to get embeddings from it?
        list_of_IDs = [1372282, 502531]

        selected_embeddings = model.get('entity_embeddings_dict')

        # for pretrained pykeen model:
        # self._link_prediction_model = torch.load(embedding_model_path, map_location = self._device)

        self._link_prediction_model = model
        # accesses the specific classifier object
        # creates: self._target_relation_classifier
        # IMPORTANT create new model or load trained checkpoint
        self.set_target_relation_classifier(model_type = model_type,
                                            trained_classifier_filename = trained_classifier_filename,
                                            load_trained_classifier = load_trained_classifier,
                                            **model_kwargs)

        self._optimizer = torch.optim.Adam(self._target_relation_classifier.parameters(), lr = lr)

    def set_target_relation_classifier(self, trained_classifier_filename: str,
                                       load_trained_classifier: bool, model_type = 'mlp',
                                       **model_kwargs):
        """

        Parameters
        ----------
        type (str): hardcoded to 'mlp'
        type
        model_kwargs

        Returns
        -------
        self._target_relation_classifier is assigned
        """
        output_layer_size = 1 if self.binary else self.num_classes
        if model_type == 'mlp':
            if load_trained_classifier:
                logging.info(
                    f'Loading trained target relation classifier: {trained_classifier_filename}')
                device = self._device
                loaded_pytorch_model = torch.load(trained_classifier_filename,
                                                  map_location = device)
                assert issubclass(type(loaded_pytorch_model), torch.nn.Module)
                self._target_relation_classifier = loaded_pytorch_model
            else:
                if "hdims" in model_kwargs:
                    hidden_layer_sizes = model_kwargs["hdims"]
                else:
                    hidden_layer_sizes = [256, 16]
                # create list of relevant dimensions
                # original code: all_layer_dims = [self._link_prediction_model.embedding_dim] + hidden_layer_sizes + [output_layer_size]
                all_layer_dims = [self._link_prediction_model.get(
                    'embedding_dim')] + hidden_layer_sizes + [output_layer_size]
                # actually instantiate the classifier
                self._target_relation_classifier = MLP(all_layer_dims, device = self._device)
        elif model_type == 'rf':
            from sklearn.ensemble import RandomForestClassifier
            self._target_relation_classifier = RandomForestClassifier(**model_kwargs)
            raise ValueError('Using the wrong class for a RF classifier!')

    def attach_classification_metrics(self):
        """


        Returns
        -------
        modifies self._trainer: attaches loss
        modifies self._evaluator
        """
        def get_loss_from_y_pred(output):
            # output is what process_function/eval_function return
            # this is y_pred and y
            y_pred, y = output

            # copied from Keidars original process_function() which returned loss
            with torch.no_grad():
                ce_loss = self._loss(y_pred, y)
                loss = ce_loss.item()

            logger.debug(
                f'Loss calculated inside get_loss_from_y_pred() is: {round(loss, 5)}')

            return loss

        ### training metrics (on training dataset)
        training_avg_loss = RunningAverage(output_transform = get_loss_from_y_pred)
        training_avg_loss.attach(self._trainer, 'training_loss')

        training_accuracy = Accuracy()
        training_accuracy.attach(self._trainer, 'training_accuracy')

        training_precision = Precision(average = False)
        training_precision.attach(self._trainer, 'training_precision')

        training_recall = Recall(average = False)
        training_recall.attach(self._trainer, 'training_recall')

        training_F1 = (training_precision * training_recall * 2 / (training_precision + training_recall)).mean()
        training_F1.attach(self._trainer, 'training_F1')

        ### evaluation metrics (on validation dataset)
        validation_avg_loss = RunningAverage(output_transform = get_loss_from_y_pred)
        validation_avg_loss.attach(self._evaluator, 'validation_loss')

        validation_accuracy = Accuracy()
        validation_accuracy.attach(self._evaluator, 'validation_accuracy')

        validation_precision = Precision(average = False)
        validation_precision.attach(self._evaluator, 'validation_precision')

        validation_recall = Recall(average = False)
        validation_recall.attach(self._evaluator, 'validation_recall')

        validation_F1 = (validation_precision * validation_recall * 2 / (validation_precision + validation_recall)).mean()
        validation_F1.attach(self._evaluator, 'validation_F1')

    def set_loss(self, binary):
        # set the loss function based on the number of classes

        if binary:
            self._loss = nn.BCEWithLogitsLoss()
        else:
            self._loss = nn.CrossEntropyLoss()

    def predict_tails(self, heads, relation):
        """
        Accesses the trained occupation classification model self._target_relation_classifier

        Parameters
        ----------
        heads
        relation
        Returns
        -------

        """
        assert type(relation) == type(self._target)
        if relation != self._target:
            logger.info("predicting tails for wrong relation, prediction is for", self._target)
        heads = heads.to(self._device)
        # original code: head_embeddings = self._link_prediction_model.entity_embeddings(heads)
        head_embeddings = torch.stack(
            [self._link_prediction_model.get('entity_embeddings_dict')[x.item()] for x in heads])
        yhat = self._target_relation_classifier(head_embeddings).detach().cpu()
        if self.binary:
            return torch.round(torch.sigmoid(yhat))  # need to put in cpu if trained on gpu...
        else:
            return torch.argmax(yhat, 1)

    def set_data_loaders(self, target_relation):
        # IMPORTANT possible to extend this to multiple target relations!
        only_keep_relations = target_relation

        logger.info(f'Target relation(s) in use: {target_relation}')

        # new_with_restriction(): only keep facts that have relation = target relation (usually occupation)
        self.train_triples_factory = train_triples_factory = self._dataset.training.new_with_restriction(
            relations = only_keep_relations)
        # create a SHUFFLED pytorch Dataloader using the train triples
        # create_lcwa_instances(): store triples in sparse format
        # In: self._train_loader.dataset[0]
        # Out: (array([ 13, 191]), array([0., 0., 0., ..., 0., 0., 0.], dtype=float32))
        self._train_loader = DataLoader(dataset = train_triples_factory.create_lcwa_instances(),
                                        batch_size = self._batch_size, shuffle = True)

        self.valid_triples_factory = valid_triples_factory = self._dataset.validation.new_with_restriction(
            relations = only_keep_relations)
        # create a NOT SHUFFLED pytorch Dataloader
        self._valid_loader = DataLoader(dataset = valid_triples_factory.create_lcwa_instances(),
                                       batch_size = self._batch_size, shuffle = False)

        self.test_triples_factory = test_triples_factory = self._dataset.testing.new_with_restriction(
            relations = only_keep_relations)
        # create a NOT SHUFFLED pytorch Dataloader
        self._test_loader = DataLoader(dataset = test_triples_factory.create_lcwa_instances(),
                                       batch_size = self._batch_size, shuffle = False)

        logger.debug(
            f'Training triples factory has {self.train_triples_factory.num_triples} triples.')
        logger.debug(f'Validation triples factory has {self.valid_triples_factory.num_triples} triples.')
        logger.debug(f'Test triples factory has {self.test_triples_factory.num_triples} triples.')

        # TODO current version 1.6 of pykeen: LCWAInstances does not have attribute labels
        # TODO Why are the labels reshaped here?
        # self._train_loader.dataset.labels = self._train_loader.dataset.labels.reshape(
        #   self._train_loader.dataset.labels.shape[0])
        # self._test_loader.dataset.labels = self._test_loader.dataset.labels.reshape(
        #    self._test_loader.dataset.labels.shape[0])

    def set_target_labels(self):
        """
        As multiclass classification is challenging for many classes and imbalanced data,
        the occupation prediction task is simplified to a smaller number of classes.

        Uses the function tails2keep() to identify the labels of the (self.num_classes - 1)
        most frequent tails.
        Each of the these tails gets a numeric class label assigned, a numeric class label for
        the OTHER class is also added.

        Returns
        -------
        self._target2int: dict of mapping occupation tail value Ids to numeric class labels
            example: {4396: 0, 11852: 1, 1370: 2, 4441: 3, 6121: 4, -1: 5}
            --> key -1 is set by self.OTHER, the other keys are pykeen entity IDs
        self.tailCounts: Counter object for all entities in the training dataset.
        """
        train_triples = self.train_triples_factory.triples
        # select only the tail values
        tails = train_triples[:, 2]
        # retrieve the (num_classes - 1) most frequent target relatin tail values as strings
        tails2keep_labels = self.tails2keep(tails)
        # retrieve the respective numeric pykeen IDs for each of the target entity labels
        # IMPORTANT: original code used self._train_loader.dataset.entity_to_id
        # BUT: current version 1.6 of pykeen: LCWAInstances does not have attribute entity_to_id
        tails2keep_IDs = [self.train_triples_factory.entity_to_id[vl] for vl in tails2keep_labels]
        # assign numeric class labels to each entity (e.g.
        self._target2int = {idval: k for k, idval in enumerate(tails2keep_IDs)}
        # to this dict, add a numeric ID (+1) for the self.OTHER class
        self._target2int[self.OTHER] = len(
            tails2keep_IDs)  # IMPORTANT: commented out, because only used inside unused make_one2one()  # for each entity inside tails, add its count in the training dataset as dict value  # self.tailCounts = Counter([self.train_triples_factory.entity_to_id[vl] for vl in tails])

    def train(self, epochs):

        # both functions need engine, batch as arguments
        self._trainer = Engine(self.process_function)
        self._evaluator = Engine(self.eval_function)

        self.attach_classification_metrics()

        self._pbar = ProgressBar(persist = True, bar_format = '')
        self._pbar.attach(self._trainer, ['loss'])

        ### Define what should happen w.r.t. logging + saving before running training!

        # save the classifier object every couple epochs in case overfitting occurs
        @self._trainer.on(Events.EPOCH_COMPLETED(every = 1))
        def save_model_with_torch_save(engine):
            # Run evaluation on validation dataset after each epoch

            metrics = self._trainer.state.metrics
            logger.info(
                f'Current Time: {datetime.now().strftime("%d.%m.%Y %H:%M")}\n'
                f'Epoch {engine.state.epoch} ran for {round(engine.state.times["EPOCH_COMPLETED"]/60, 2)} min \n'
                f'Training Metrics:\n {pprint.pformat(metrics)}')

            # needs validation loader as argument
            self._evaluator.run(self._valid_loader)
            logger.info(f'Evaluation Metrics:\n {pprint.pformat(self._evaluator.state.metrics)}')

            assert issubclass(type(self._target_relation_classifier), nn.Module)
            torch.save(self._target_relation_classifier, f'trained_classifier_e_{engine.state.epoch}.pt')

        #@self._trainer.on(Events.EPOCH_COMPLETED(every = 1))
        #def run_validation_after_each_epoch(engine):


        # save the trainer object every couple epochs to resume training later on
        model_save_handler = ModelCheckpoint(dirname = os.getcwd(),
                                             # save to existing logging directory
                                             filename_prefix = 'trainer_engine', include_self = True,
                                             # save state_dict
                                             n_saved = None  # if None, keep all saved checkpoints
                                             )
        self._trainer.add_event_handler(Events.EPOCH_COMPLETED(every = 5), model_save_handler,
                                        {'trainer': self._trainer})

        # add tensorboard logging for metrics + weights
        tb_logger = TensorboardLogger(log_dir = os.getcwd(), flush_secs = 30,
                                      filename_suffix = f'_train_log')
        tb_logger.attach(
            self._trainer,
            event_name = Events.EPOCH_COMPLETED,
            log_handler = OutputHandler(
                tag = 'training',
                metric_names = 'all'  # using 'all' is fine, but typechecking complains anyway
            )
        )
        tb_logger.attach(
            self._evaluator,
            event_name = Events.EPOCH_COMPLETED,
            log_handler = OutputHandler(
                tag = 'validation',
                metric_names = 'all'  # using 'all' is fine, but typechecking complains
            )
        )
        # log weights as histograms after each epoch
        tb_logger.attach(
            self._trainer,
            event_name = Events.EPOCH_COMPLETED,
            log_handler = WeightsHistHandler(self._target_relation_classifier)
        )
        # log gradients as histograms after each epoch
        tb_logger.attach(
            self._trainer,
            event_name = Events.EPOCH_COMPLETED,
            log_handler = GradsHistHandler(self._target_relation_classifier)
        )

        # Run training (evaluation is run inside)
        self._trainer.run(self._train_loader, max_epochs = epochs)
        tb_logger.close()


    def process_function(self, engine, batch):
        """
        This is passed to Engine and stored as self._trainer within  train().
        This function specifies what should happen to each batch of the dataset.

        Parameters
        ----------
        engine (ignite.Engine):
        batch

        Returns
        -------

        """
        self._target_relation_classifier.to(self._device)
        self._target_relation_classifier.train()

        # heads,tails are both torch.Tensor, dtype = int64
        heads, tails = self.get_heads_tails(batch)
        labels = torch.Tensor([self.target2label(tl) for tl in tails])
        # change labels tensor to respective format and send to device
        assert labels.dtype == torch.float
        if self.binary:
            labels.to(self._device)
        else:
            labels = labels.long().to(self._device)
        # original code: embeddings = self._link_prediction_model.entity_embeddings(heads.to(self._device))
        # TODO remove hacky code: extract embeddings as list of tensors from the graphvite dict....
        embeddings = torch.stack(
            [self._link_prediction_model.get('entity_embeddings_dict')[x.item()] for x in heads])
        # doublecheck the input
        # TODO why is the below assert statement not true?
        # assert embeddings.size()[0] == self._batch_size
        assert embeddings.size()[1] == self._link_prediction_model.get('embedding_dim')
        assert type(embeddings) == torch.Tensor
        logits = self._target_relation_classifier(embeddings.to(self._device))

        ce_loss = self._loss(logits, labels)

        self._optimizer.zero_grad()
        ce_loss.mean().backward()
        self._optimizer.step()

        # IMPORTANT: Output required to calculate classficiation metrics using pytorch ignite is:
        y_pred = logits
        y = labels

        logger.debug(f'Loss calculated inside process_function() is: {round(ce_loss.item(), 5)}')


        return y_pred, y

    def eval_function(self, engine, batch):
        """
        This is passed to Engine and stored as self._evaluator within train().
        This function specifies what should happen to each batch of the dataset.

        Parameters
        ----------
        engine
        batch

        Returns
        -------

        """
        self._target_relation_classifier.eval()
        heads, tails = self.get_heads_tails(batch)

        labels = self.targets2labels(tails)
        labels = labels.type(dtype = torch.int64).to(self._device)

        with torch.no_grad():
            # original code: embeddings = self._link_prediction_model.entity_embeddings(heads.to(self._device))
            embeddings = torch.stack(
                [self._link_prediction_model.get('entity_embeddings_dict')[x.item()] for x in
                 heads])

            # IMPORTANT: Output required to calculate classification metrics using pytorch ignite is:
            y_pred = self._target_relation_classifier.predict(embeddings.to(self._device))
            y = labels

            return y_pred, y

    def get_heads_tails(self, batch):
        """
        Split batch into heads and tails
        """
        data, targets = batch
        data, targets = data, targets
        heads = data[:, 0]

        tails_idx = (targets == 1).nonzero(as_tuple = False)[:, 0]
        tails = (targets == 1).nonzero(as_tuple = False)[:, 1]
        tails_list = [tl.item() for tl in tails]
        if heads.shape != tails.shape:
            # if the number of heads doesn't match the number of tails,
            # Choose one tail per head so
            # tails_list = self.make_one2one(tails_list, tails_idx, batch_size=len(heads))
            heads = self.increase_heads(tails, tails_idx, heads)
        return heads, tails_list

    def increase_heads(self, tails, tails_idx, heads):
        """"
        Make sure each entity corresponds to one tail
        """
        new_heads = np.zeros(tails.shape, )
        for idx, (tail, tail_idx) in enumerate(zip(tails, tails_idx)):
            # if current and previous tail belong to the same target
            new_heads[idx] = heads[tail_idx]  # choose the more frequent tail
        return torch.LongTensor(new_heads)

    # def make_one2one(self, tails, tails_idx, batch_size):
    #     """"
    #     Make sure each entity corresponds to one tail
    #     """
    #     new_tails = np.zeros(batch_size,)
    #     prev_idx = -1
    #     for tail, idx in zip(tails, tails_idx):
    #         # if current and previous tail belong to the same target
    #         if idx == prev_idx:
    #             cur_tail = new_tails[idx]
    #             # choose the more frequent tail
    #             if self.tailCounts[cur_tail] <= self.tailCounts[tail]:
    #                 new_tails[idx] = tail
    #         else:
    #             prev_idx = idx
    #             new_tails[idx] = tail
    #     return new_tails

    def tails2keep(self, tails):
        """
        As multiclass classification is challenging for many classes and imbalanced data,
        the occupation prediction task is simplified to a smaller number of classes.

        From the tail values of the original train triples (with relation = target relation, e.g. occupation)
        only the (self.num_classes - 1) most frequent tails are kept.

        Returns
        -----------
        keep (list): Contains the strings/labels of the tail values to keep in descending
            frequency.
            Length of list is (self.num_classes - 1)

        """
        # If there are less tail types than num_classes, don't do anything
        if len(set(tails)) <= self.num_classes:
            self.num_classes = len(set(tails))
            return tails
        # TODO add a loop through relations, if you use multiple target relations!
        # count the occurrence of each tail value
        tail_count = Counter(tails)
        # Choose which tails to keep
        keep = []
        for keep_tail in range(self.num_classes - 1):
            # find the tail value inside tail_count with highest count
            cur_max = max(tail_count.items(), key = operator.itemgetter(1))[0]
            # add it to the list of tail values to keep
            keep.append(cur_max)
            # delete current max so we'll find a different one in the next iteration
            del tail_count[
                cur_max]  ## do this instead when debugging:  # del tail_count[cur_max[0]]
        return keep

    def targets2labels(self, targets):
        labels = []
        for tail in targets:
            if tail in self._target2int.keys():
                labels.append(int(self._target2int[tail]))
            else:
                labels.append(int(self._target2int[self.OTHER]))
        return torch.Tensor(labels)

    def target2label(self, target):
        if target in self._target2int.keys():
            return int(self._target2int[target])
        else:
            return int(self._target2int[self.OTHER])


class RFRelationClassifier:

    def __init__(self, dataset, target_relation, embedding_model_path, batch_size, num_classes = 6,
                 **model_kwargs):
        """"
        embedding_model_path : path to the kg embedding that will be used
        num_classes : number of labels to consider. The classifier will learn
            to predict the (num_classes -1) most frequent labels,
            and consider all the rest to be of class OTHER
        hidden_layer_sizes
        batch_size : the batch size used when training
        """
        # IMPORTANT make this class label obviously different than the others!
        self.OTHER = -1

        self._device = self.DEVICE = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self._dataset = dataset
        self.num_classes = num_classes
        self._batch_size = batch_size
        self._target = target_relation

        self.binary = (num_classes == 2)

        # retrieve train + test triple factories and DataLoaders
        # creates: self.train_triples_factory, self._train_loader, self.test_triples_factory, self._test_loader
        self.set_data_loaders(target_relation = target_relation)

        # retrieve (num_classes -1) most frequent tail values (after target relation)
        # and map them + self.OTHER to numeric class labels
        # creates: self._target2int
        self.set_target_labels()

        # load trained (pykeen) model from path
        # will be used by: train(), predict_tails()
        self._link_prediction_model = torch.load(embedding_model_path, map_location = self._device)
        # accesses the specific classifier object
        # creates: self._target_relation_classifier
        self.set_target_relation_classifier(**model_kwargs)

    def set_target_relation_classifier(self, **model_kwargs):
        from sklearn.ensemble import RandomForestClassifier
        self._target_relation_classifier = RandomForestClassifier(warm_start = True, **model_kwargs)

    def predict_tails(self, heads, relation):
        if relation != self._target:
            logger.info("predicting tails for wrong relation, prediction is for", self._target)
        heads = heads.to(self._device)
        head_embeddings = self._link_prediction_model.entity_embeddings(heads).detach().numpy()
        yhat = self._target_relation_classifier.predict(head_embeddings)
        return yhat

    def set_data_loaders(self, target_relation):
        # IMPORTANT possible to extend this to multiple target relations!
        only_keep_relations = [target_relation]

        # new_with_restriction(): only keep facts that have relation = target relation (usually occupation
        self.train_triples_factory = train_triples_factory = self._dataset.training.new_with_restriction(
            relations = only_keep_relations)

        # create a SHUFFLED pytorch Dataloader using the train triples
        # create_lcwa_instances(): store triples in sparse format
        # In: self._train_loader.dataset[0]
        # Out: (array([ 13, 191]), array([0., 0., 0., ..., 0., 0., 0.], dtype=float32))
        self._train_loader = DataLoader(dataset = train_triples_factory.create_lcwa_instances(),
                                        batch_size = self._batch_size, shuffle = True)
        self.test_triples_factory = test_triples_factory = self._dataset.testing.new_with_restriction(
            relations = only_keep_relations)

        # create a NOT SHUFFLED pytorch Dataloader
        self._test_loader = DataLoader(dataset = test_triples_factory.create_lcwa_instances(),
                                       batch_size = self._batch_size, shuffle = False)

        # TODO current version 1.6 of pykeen: LCWAInstances does not have attribute labels  # TODO Why are the labels reshaped here?  # self._train_loader.dataset.labels = self._train_loader.dataset.labels.reshape(  #     self._train_loader.dataset.labels.shape[0])  # self._test_loader.dataset.labels = self._test_loader.dataset.labels.reshape(  #     self._test_loader.dataset.labels.shape[0])

    def set_target_labels(self):
        """
        As multiclass classification is challenging for many classes and imbalanced data,
        the occupation prediction task is simplified to a smaller number of classes.

        Uses the function tails2keep() to identify the labels of the (self.num_classes - 1)
        most frequent tails.
        Each of the these tails gets a numeric class label assigned, a numeric class label for
        the OTHER class is also added.

        Returns
        -------
        self._target2int: dict of mapping occupation tail value Ids to numeric class labels
            example: {4396: 0, 11852: 1, 1370: 2, 4441: 3, 6121: 4, -1: 5}
            --> key -1 is set by self.OTHER, the other keys are pykeen entity IDs
        self.tailCounts: Counter object for all entities in the training dataset.
        """
        train_triples = self.train_triples_factory.triples
        # select only the tail values
        tails = train_triples[:, 2]
        # retrieve the (num_classes - 1) most frequent target relatin tail values as strings
        tails2keep_labels = self.tails2keep(tails)
        # retrieve the respective numeric pykeen IDs for each of the target entity labels
        # IMPORTANT: original code used self._train_loader.dataset.entity_to_id
        # BUT: current version 1.6 of pykeen: LCWAInstances does not have attribute entity_to_id
        tails2keep_IDs = [self.train_triples_factory.entity_to_id[vl] for vl in tails2keep_labels]
        # assign numeric class labels to each entity (e.g.
        self._target2int = {idval: k for k, idval in enumerate(tails2keep_IDs)}
        # to this dict, add a numeric ID (+1) for the self.OTHER class
        self._target2int[self.OTHER] = len(
            tails2keep_IDs)  # IMPORTANT: commented out, because only used inside unused make_one2one()  # for each entity inside tails, add its count in the training dataset as dict value  # self.tailCounts = Counter([self.train_triples_factory.entity_to_id[vl] for vl in tails])

    def train(self):
        for batch in self._train_loader:
            heads, tails = self.get_heads_tails(batch)
            heads = self._link_prediction_model.entity_embeddings(
                heads.to(self._device)).detach().numpy()
            labels = self.targets2labels(tails)
            labels = labels.type(dtype = torch.int).to(self._device).detach().numpy()
            self._target_relation_classifier.n_estimators += 11
            self._target_relation_classifier.fit(heads, labels)

    def get_heads_tails(self, batch):
        """
        Split batch into heads and tails
        """
        data, targets = batch
        data, targets = data, targets
        heads = data[:, 0]

        tails_idx = (targets == 1).nonzero(as_tuple = False)[:, 0]
        tails = (targets == 1).nonzero(as_tuple = False)[:, 1]
        tails_list = [tl.item() for tl in tails]
        if heads.shape != tails.shape:
            # if the number of heads doesn't match the number of tails,
            # Choose one tail per head so
            # tails_list = self.make_one2one(tails_list, tails_idx, batch_size=len(heads))
            heads = self.increase_heads(tails, tails_idx, heads)
        return heads, tails_list

    def increase_heads(self, tails, tails_idx, heads):
        """"
        Make sure each entity corresponds to one tail
        """
        new_heads = np.zeros(tails.shape, )
        for idx, (tail, tail_idx) in enumerate(zip(tails, tails_idx)):
            # if current and previous tail belong to the same target
            new_heads[idx] = heads[tail_idx]  # choose the more frequent tail
        return torch.LongTensor(new_heads)

    # def make_one2one(self, tails, tails_idx, batch_size):
    #     """"
    #     Make sure each entity corresponds to one tail
    #     """
    #     new_tails = np.zeros(batch_size, )
    #     prev_idx = -1
    #     for tail, idx in zip(tails, tails_idx):
    #         # if current and previous tail belong to the same target
    #         if idx == prev_idx:
    #             cur_tail = new_tails[idx]
    #             # choose the more frequent tail
    #             if self.tailCounts[cur_tail] <= self.tailCounts[tail]:
    #                 new_tails[idx] = tail
    #         else:
    #             prev_idx = idx
    #             new_tails[idx] = tail
    #     return new_tails

    def tails2keep(self, tails):
        """
        As multiclass classification is challenging for many classes and imbalanced data,
        the occupation prediction task is simplified to a smaller number of classes.

        From the tail values of the original train triples (with relation = target relation, e.g. occupation)
        only the (self.num_classes - 1) most frequent tails are kept.

        Returns
        -----------
        keep (list): Contains the strings/labels of the tail values to keep in descending
            frequency.
            Length of list is (self.num_classes - 1)

        """
        # If there are less tail types than num_classes, don't do anything
        if len(set(tails)) <= self.num_classes:
            self.num_classes = len(set(tails))
            return tails
        # TODO add a loop through relations, if you use multiple target relations!
        # count the occurrence of each tail value
        tail_count = Counter(tails)
        # Choose which tails to keep
        keep = []
        for keep_tail in range(self.num_classes - 1):
            # find the tail value inside tail_count with highest count
            cur_max = max(tail_count.items(), key = operator.itemgetter(1))[0]
            # add it to the list of tail values to keep
            keep.append(cur_max)
            # delete current max so we'll find a different one in the next iteration
            del tail_count[
                cur_max]  ## do this instead when debugging:  # del tail_count[cur_max[0]]
        return keep

    def targets2labels(self, targets):
        labels = []
        for tail in targets:
            if tail in self._target2int.keys():
                labels.append(int(self._target2int[tail]))
            else:
                labels.append(int(self._target2int[self.OTHER]))
        return torch.Tensor(labels)

    def target2label(self, target):
        if target in self._target2int.keys():
            return int(self._target2int[target])
        else:
            return int(self._target2int[self.OTHER])
