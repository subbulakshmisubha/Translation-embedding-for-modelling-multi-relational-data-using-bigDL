import sys
import time
import random
import numpy as np
import pandas as pd
from bigdl.nn.layer import *
from bigdl.util.common import *
from bigdl.nn.criterion import *
from bigdl.optim.optimizer import *
import logging
from pyspark import SparkContext, SparkConf

init_engine()


def create_model(total_embeddings, embedding_dim=50):
    global embedding
    model = Sequential()
    model.add(Reshape([6]))
    embedding = LookupTable(total_embeddings, embedding_dim)
    model.add(embedding)
    model.add(Reshape([2, 3, 1, embedding_dim])).add(Squeeze(1))
    model.add(SplitTable(2))

    branches = ParallelTable()
    branch1 = Sequential()
    pos_h_l = Sequential().add(ConcatTable().add(Select(2, 1)).add(Select(2, 3)))
    pos_add = pos_h_l.add(CAddTable())
    pos_t = Sequential().add(Select(2, 2)).add(MulConstant(-1.0))
    triple_pos_meta = Sequential().add(ConcatTable().add(pos_add).add(pos_t))
    triple_pos_dist = triple_pos_meta.add(CAddTable()).add(Abs())
    triple_pos_score = triple_pos_dist.add(Unsqueeze(1)).add(Mean(4, 1)).add(MulConstant(float(embedding_dim)))
    branch1.add(triple_pos_score).add(Squeeze(3)).add(Squeeze(1)).add(Unsqueeze(2))

    branch2 = Sequential()
    neg_h_l = Sequential().add(ConcatTable().add(Select(2, 1)).add(Select(2, 3)))
    neg_add = neg_h_l.add(CAddTable())
    neg_t = Sequential().add(Select(2, 2)).add(MulConstant(-1.0))
    triple_neg_meta = Sequential().add(ConcatTable().add(neg_add).add(neg_t))
    triple_neg_dist = triple_neg_meta.add(CAddTable()).add(Abs())
    triple_neg_score = triple_neg_dist.add(Unsqueeze(1)).add(Mean(4, 1)).add(MulConstant(float(embedding_dim)))
    branch2.add(triple_neg_score).add(Squeeze(3)).add(Squeeze(1)).add(Unsqueeze(2))

    branches.add(branch1).add(branch2)
    model.add(branches)
    return model


class TransE:
    def __init__(self, entity_dict, train_triples, test_triples, margin=1, learning_rate=0.01, dim=50):
        self.entity_dict = entity_dict
        self.train_triples = train_triples
        self.test_triples=test_triples
        self.training_triple_pool = set(train_triples)
        self.margin = margin
        self.learning_rate = learning_rate
        self.dim = dim
        self.batch_pos = []
        self.batch_neg = []
        self.batch_total = []
        self.total_embeddings = 0
        self.corrupted_triplets = []

    def training(self, total_embeddings):
        sample = np.array(self.batch_total)
        sample_rdd = sc.parallelize(sample)
        train_data = sample_rdd.map(lambda t: Sample.from_ndarray(t, labels=[np.array(1.), np.array(1.)]))
        print("Train Data", train_data.take(2))
        model = create_model(total_embeddings)
        print(model.parameters())

        print("Start Training")
        start = time.time()
        optimizer = Optimizer(
            model=model,
            training_rdd=train_data,
            criterion=MarginRankingCriterion(),
            optim_method=SGD(learningrate=0.01, learningrate_decay=0.001, weightdecay=0.001,
                             momentum=0.0, dampening=DOUBLEMAX, nesterov=False,
                             leaningrate_schedule=None, learningrates=None,
                             weightdecays=None, bigdl_type="float"),
            end_trigger=MaxEpoch(1000),
            batch_size=128)
        trained_model = optimizer.optimize()

        print(trained_model.parameters())
        stop = time.time()
        print("Training is completed")
        print("Training took", stop-start, "seconds")
        print("Saving trained model")
        trained_model.saveModel("/home/heena/Documents/Distributed-Big-Data-Lab-Project/data/model.bigdl", "/home/heena/Documents/Distributed-Big-Data-Lab-Project/data/model.bin", True)

    def testing(self):
        count = 0
        head_rank_raw = 0
        tail_rank_raw = 0
        head_hits10_raw = 0
        tail_hits10_raw = 0
        test_samples = self.corrupted_triplets

        model = Model.loadModel("/home/heena/Documents/Distributed-Big-Data-Lab-Project/data/model.bigdl","/home/heena/Documents/Distributed-Big-Data-Lab-Project/data/model.bin")
        predmodel = Sequential().add(model).add(JoinTable(2, 2))

        for sample in test_samples:
            test_rdd = sc.parallelize(np.array(sample))
            # print(test_rdd.take(10))
            test_data = test_rdd.map(lambda t: Sample.from_ndarray(t, labels=[np.array(1.), np.array(1.)]))
            # print(test_data.take(10))

            result = predmodel.predict(test_data)
            # print(result.take(13000))
            # predicted_score= result.sortBy(lambda y: -y[1])
            # print("score", predicted_score.take(10))
            # print(result.take(2))
            score = result.map(lambda t: t[0] - t[1])
            # print(score.take(14982))
            # print(score.count())
            # score.persist()
            # print(score.take(2))
            sorted_score = score.sortBy(lambda x: x).zipWithIndex()
            # print(sorted_score.take(2))
            rank = sorted_score.filter(lambda x: x[0] == 0).map(lambda x: x[1]).take(1)
            # print("Result", rank[0])
            if count % 2 == 0:
                head_rank_raw += rank[0]
                if (rank[0]) < 10:
                    head_hits10_raw += 1

            else:
                tail_rank_raw += rank[0]
                if (rank[0]) < 10:
                    tail_hits10_raw += 1

            count = count + 1

        mean_rank = (head_rank_raw + tail_rank_raw) / len(test_samples)
        hits10 = (head_hits10_raw + tail_hits10_raw) / len(test_samples)
        print("Mean Rank: ", mean_rank)
        print("Hits@10: ", hits10)

    def generate_training_corrupted_triplets(self, cycle_index=1):
        count = 0
        for i in range(cycle_index):

            if count == 0:
                start_time = time.time()
            count += 1
            Sbatch = self.train_triples

            raw_batch = Sbatch
            if raw_batch is None:
                return
            else:
                batch_pos = raw_batch
                batch_neg = []
                batch_total = []
                for head, tail, relation in batch_pos:
                    corrupt_head_prob = np.random.binomial(1, 0.5)
                    head_neg = head
                    tail_neg = tail
                    while True:
                        if corrupt_head_prob:
                            head_neg = random.choice(list(self.entity_dict.values()))
                        else:
                            tail_neg = random.choice(list(self.entity_dict.values()))
                        if (head_neg, tail_neg, relation) not in (self.training_triple_pool and self.batch_neg):
                            break
                    batch_pos = [head,tail,relation,head_neg,tail_neg,relation]
                    batch_total.append(batch_pos)
            self.batch_total+=batch_total

    def generate_test_corrupted_triplets(self):
        self.corrupted_triplets = []
        Sbatch = self.test_triples

        if Sbatch is None:
            return

        for head, tail, relation in Sbatch:
            corrupt_entity_list = list(self.entity_dict.values())
            corrupted_test_head_triplets = []
            corrupted_test_tail_triplets = []

            for i in range(0, len(corrupt_entity_list)):
                if (corrupt_entity_list[i], tail, relation) not in self.training_triple_pool:
                    corrupted_test_head = [head, tail, relation, corrupt_entity_list[i], tail, relation]
                    corrupted_test_head_triplets.append(corrupted_test_head)
                else:
                    continue

            for i in range(0, len(corrupt_entity_list)):
                if (head, corrupt_entity_list[i], relation) not in self.training_triple_pool:
                    corrupted_test_tail = [head, tail, relation, head, corrupt_entity_list[i], relation]
                    corrupted_test_tail_triplets.append(corrupted_test_tail)
                else:
                    continue

            self.corrupted_triplets.append(corrupted_test_head_triplets)
            self.corrupted_triplets.append(corrupted_test_tail_triplets)


if __name__ == "__main__":
    s_logger = logging.getLogger('py4j.java_gateway')
    s_logger.setLevel(logging.ERROR)
    conf=SparkConf().setAppName('test').setMaster('local[*]')
    sc = SparkContext.getOrCreate(conf)
    sc.setLogLevel("ERROR")
    entities = pd.read_table("/home/heena/Documents/Distributed-Big-Data-Lab-Project/data/FB15k/entity2id.txt", header=None)
    dict_entities = dict(zip(entities[0], entities[1]+1))
    relations = pd.read_table("/home/heena/Documents/Distributed-Big-Data-Lab-Project/data/FB15k/relation2id.txt", header=None)
    dict_relations = dict(zip(relations[0], relations[1]+1))
    training_df = pd.read_table("/home/heena/Documents/Distributed-Big-Data-Lab-Project/data/FB15k/train.txt", header=None)
    test_df=pd.read_table("/home/heena/Documents/Distributed-Big-Data-Lab-Project/data/FB15k/test.txt", header=None)

    training_triples = list(zip([dict_entities[h] for h in training_df[0]],
                                 [dict_entities[t]for t in training_df[1]],
                                  [dict_relations[r]+len(entities) for r in training_df[2]]))

    testing_triples=list(zip([dict_entities[h] for h in test_df[0]],
                                [dict_entities[t] for t in test_df[1]],
                                [dict_relations[r] + len(entities) for r in test_df[2]]))

    transE = TransE(dict_entities, training_triples, testing_triples)
    transE.generate_training_corrupted_triplets()
    transE.total_embeddings = len(entities) + len(relations)

    transE.training(transE.total_embeddings)
    transE.generate_test_corrupted_triplets()
    transE.testing()
    sc.stop()
