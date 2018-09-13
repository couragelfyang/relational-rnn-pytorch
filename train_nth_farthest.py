"""
Implementation of 'Nth Farthest' task
as defined in Santoro et. al., 2018
(https://arxiv.org/abs/1806.01822)

Note: Work in progress

Author: Jessica Yung
August 2018

Relational Memory Core implementation mostly written by Sang-gil Lee, adapted by Jessica Yung.
"""
import torch
import torch.nn as nn
import matplotlib.pyplot as plt
import numpy as np
from argparse import ArgumentParser
from comet_ml import Experiment

from relational_rnn_general import RelationalMemory

experiment = Experiment(api_key="fQCW6qvjBmkubI28MZubfxGmy", project_name="rrnn-nth-farthest")
parser = ArgumentParser()

# Model parameters.
parser.add_argument('--cuda', type=str, default=True,
                    help='Whether to use CUDA (GPU). Default=True. (Set as 0 for False)')

parse_args = parser.parse_args()

is_cuda = bool(int(parse_args.cuda))

if is_cuda:
    exp_device = "cuda"
else:
    exp_device = "cpu"

device = torch.device(exp_device)

# network params
learning_rate = 1e-4
num_epochs = 1000000
dtype = torch.float
mlp_size = 256

# data params
num_vectors = 8
num_dims = 16
# num_examples = 10000
num_batches = 6  # set batches per epoch because we are generating data from scratch each time
                 # so now 1 epoch '=' 6 epochs, it's just the frequency of testing
# test_size = 0.2
num_test_examples = 3200

# num_train = int((1-test_size) * num_examples)
batch_size = 1600

####################
# Generate data
####################

# For each example
input_size = num_dims + num_vectors * 3

def one_hot_encode(array, num_dims=8):
    one_hot = np.zeros((len(array), num_dims))
    for i in range(len(array)):
        one_hot[i, array[i]] = 1
    return one_hot

def get_example(num_vectors, num_dims):
    input_size = num_dims + num_vectors * 3
    n = np.random.choice(num_vectors, 1)  # nth farthest from target vector
    labels = np.random.choice(num_vectors,num_vectors,replace=False)
    m_index = np.random.choice(num_vectors, 1)  # m comes after the m_index-th vector
    m = labels[m_index]

    # Vectors sampled from U(-1,1)
    vectors = np.random.rand(num_vectors, num_dims)*2 - 1
    target_vector = vectors[m_index]
    dist_from_target = np.linalg.norm(vectors - target_vector, axis=1)
    X_single = np.zeros((num_vectors, input_size))
    X_single[:, :num_dims] = vectors
    labels_onehot = one_hot_encode(labels, num_dims=num_vectors)
    X_single[:, num_dims:num_dims+num_vectors] = labels_onehot
    nm_onehot = np.reshape(one_hot_encode([n, m], num_dims=num_vectors), -1)
    X_single[:, num_dims+num_vectors:] = np.tile(nm_onehot, (num_vectors, 1))
    y_single = labels[np.argsort(dist_from_target)[-(n+1)]]

    return X_single, y_single

def get_examples(num_examples, num_vectors, num_dims, device="cuda"):
    X = np.zeros((num_examples, num_vectors, input_size))
    y = np.zeros(num_examples)
    for i in range(num_examples):
        X_single, y_single = get_example(num_vectors, num_dims)
        X[i,:] = X_single
        y[i] = y_single

    X = torch.Tensor(X).to(device)
    y = torch.LongTensor(y).to(device)
    
    return X, y

X_test, y_test = get_examples(num_test_examples, num_vectors, num_dims, device=exp_device)


class RMCArguments:
    def __init__(self):
        self.memslots = 8
        self.numheads = 8
        self.headsize = int(2048 / (self.numheads * self.memslots))
        self.input_size = input_size  # dimensions per timestep
        self.numblocks = 1
        self.forgetbias = 1.
        self.inputbias = 0.
        self.attmlplayers = 2
        self.cutoffs = [10000, 50000, 100000]
        self.batch_size = batch_size
        self.clip = 0.1

args = RMCArguments()


####################
# Build model
####################

class RRNN(nn.Module):
    def __init__(self, mlp_size):
        super(RRNN, self).__init__()
        self.mlp_size = mlp_size
        self.memory_size_per_row = args.headsize * args.numheads * args.memslots
        self.relational_memory = RelationalMemory(mem_slots=args.memslots, head_size=args.headsize, input_size=args.input_size,
                         num_heads=args.numheads, num_blocks=args.numblocks, forget_bias=args.forgetbias,
                         input_bias=args.inputbias,
                         cutoffs=args.cutoffs).to(device)
        self.relational_memory = nn.DataParallel(self.relational_memory)
        # Map from memory to logits (categorical predictions)
        self.mlp = nn.Sequential(
            nn.Linear(self.memory_size_per_row, self.mlp_size),
            nn.ReLU(),
            nn.Linear(self.mlp_size, self.mlp_size),
            nn.ReLU(),
            nn.Linear(self.mlp_size, self.mlp_size),
            nn.ReLU(),
            nn.Linear(self.mlp_size, self.mlp_size),
            nn.ReLU()
        )
        self.out = nn.Linear(self.mlp_size, num_vectors)
        self.softmax = nn.Softmax()

    def forward(self, input, memory):
        memory = self.relational_memory(input, memory)
        mlp = self.mlp(memory)
        out = self.out(mlp)
        out = self.softmax(out)

        return out

model = RRNN(mlp_size)
if is_cuda:
    model = model.cuda()
total_params = sum(p.numel() for p in model.parameters() if p.requires_grad)

print("Model built, total trainable params: " + str(total_params))

# # TODO: should forget & input bias be trainable? sonnet is not i think
# model.forget_bias.requires_grad = False
# model.input_bias.requires_grad = False

def get_batch(X, y, batch_num, batch_size=32, batch_first=True):
    if not batch_first:
        raise NotImplementedError
    start = batch_num*batch_size
    end = (batch_num+1)*batch_size
    return X[start:end], y[start:end]

loss_fn = torch.nn.CrossEntropyLoss()

optimiser = torch.optim.Adam(model.parameters(), lr=learning_rate)

scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimiser, 'min', factor=0.5, patience=5, min_lr=8e-5)

# num_batches = int(len(X_train) / batch_size)
num_test_batches = int(len(X_test) / batch_size)

memory = model.relational_memory.module.initial_state(args.batch_size, trainable=True).to(device)

hist = np.zeros(num_epochs)
hist_acc = np.zeros(num_epochs)
test_hist = np.zeros(num_epochs)
test_hist_acc = np.zeros(num_epochs)

def accuracy_score(y_pred, y_true):
    return np.array(y_pred == y_true).sum()*1.0 / len(y_true)

####################
# Train model
####################

for t in range(num_epochs):
    epoch_loss = np.zeros(num_batches)
    epoch_acc = np.zeros(num_batches)
    epoch_test_loss = np.zeros(num_test_batches)
    epoch_test_acc = np.zeros(num_test_batches)
    for i in range(num_batches):
        data, targets = get_examples(batch_size, num_vectors, num_dims, device=exp_device)
        model.zero_grad()
        # model.hidden = model.init_hidden()
        # forward pass
        y_pred = model(data, memory)

        loss = loss_fn(y_pred, targets)
        loss = torch.mean(loss)
        y_pred = torch.argmax(y_pred, dim=1)
        acc = accuracy_score(y_pred, targets)
        epoch_loss[i] = loss
        epoch_acc[i] = acc

        # Zero out gradient, else they will accumulate between epochs
        optimiser.zero_grad()

        # backward pass
        loss.backward()

        # torch.nn.utils.clip_grad_norm_(model.parameters(), args.clip)

        # update parameters
        optimiser.step()

    # test examples
    hist[t] = np.mean(epoch_loss)
    if t % 5 == 0:
        # print("train: ", y_pred, targets)
        pass
    for i in range(num_test_batches):
        with torch.no_grad():
            data, targets = get_batch(X_test, y_test, i, batch_size=batch_size)
            ytest_pred = model(data, memory)

            test_loss = loss_fn(ytest_pred, targets)
            test_loss = torch.mean(test_loss)
            ytest_pred = torch.argmax(ytest_pred, dim=1)
            test_acc = accuracy_score(ytest_pred, targets)
            epoch_test_loss[i] = test_loss
            epoch_test_acc[i] = test_acc

    loss = np.mean(epoch_loss)
    acc = np.mean(epoch_acc)
    test_loss = np.mean(epoch_test_loss)
    test_acc = np.mean(epoch_test_acc)

    hist[t] = loss
    hist_acc[t] = acc
    test_hist[t] = test_loss
    test_hist_acc[t] = test_acc

    # Log to Comet.ml
    experiment.log_metric("loss", loss, step=t)
    experiment.log_metric("test_loss", test_loss, step=t)
    experiment.log_metric("accuracy", acc, step=t)
    experiment.log_metric("test_accuracy", test_acc, step=t)

    if t % 10 == 0:
        # print(epoch_test_loss)
        # print(epoch_test_acc)
        print("Epoch {} train loss: {}".format(t, loss))
        print("Epoch {} test  loss: {}".format(t, test_loss))
        print("Epoch {} train  acc: {:.2f}".format(t, acc))
        print("Epoch {} test   acc: {:.2f}".format(t, test_acc))
        # print("test: ", ytest_pred, targets)

##############
######
# Plot losses
####################

plt.plot(hist, label="Training loss")
plt.plot(test_hist, label="Test loss")
plt.legend()
plt.title("Cross entropy loss")
plt.show()

# Plot accuracy
plt.plot(hist_acc, label="Training accuracy")
plt.plot(test_hist_acc, label="Test accuracy")
plt.title("Accuracy")
plt.legend()
plt.show()


