"""
Module for evaluating MMD generative models.

Yujia Li, 11/2014
"""

import cPickle as pickle
import time
import numpy as np
import gnumpy as gnp
import core.generative as gen
import core.kernels as ker

def load_tfd_fold(fold=0):
    """
    Return train, val, test data for the particular fold.
    """
    import dataio.tfd as tfd
    # note that the training set used here is the 'unlabeled' set in TFD
    x_train, _, _ = tfd.load_fold(fold, 'unlabeled', scale=True)
    x_val,   _, _ = tfd.load_fold(fold, 'val', scale=True)
    x_test,  _, _ = tfd.load_fold(fold, 'test', scale=True)

    imsz = np.prod(x_train.shape[1:])

    return x_train.reshape(x_train.shape[0], imsz), \
            x_val.reshape(x_val.shape[0], imsz), \
            x_test.reshape(x_test.shape[0], imsz)

def linear_classifier_discrimination(model, data, C_range=[1], verbose=True, samples=None):
    """
    Compute the logistic regression classification accuracy.
    """
    import sklearn.linear_model as lm
    n_examples = data.shape[0]
    if samples is None:
        gnp.seed_rand(8)
        samples = model.generate_samples(n_samples=n_examples).asarray()

    x = np.r_[data, samples]
    t = np.r_[np.zeros(n_examples, dtype=np.int), np.ones(samples.shape[0], dtype=np.int)]

    best_acc = 0
    best_classifier = None

    for C in C_range:
        t_start = time.time()
        lr = lm.LogisticRegression(C=C, dual=False, random_state=8)
        lr.fit(x,t)
        acc = (lr.predict(x) == t).mean()

        if verbose:
            print 'C=%g  acc=%.4f' % (C, acc),
        if acc > best_acc:
            best_acc = acc
            best_classifier = lr 
            if verbose:
                print '*',
        else:
            if verbose:
                print ' ',

        if verbose:
            print 'time=%.2f' % (time.time() - t_start)

    return best_acc, best_classifier

def eval_filter_thresholds(model, data, thres_range=np.arange(0, 0.9, 0.1)):
    """
    Evaluate the discrimination performance at different threshold levels.
    """
    #data = data[:1000]
    n_thres = len(thres_range)

    # base classifier
    acc, c = linear_classifier_discrimination(model, data, verbose=False)
    acc_rec = np.zeros(n_thres, dtype=np.float)
    acc_best = 1
    for i in range(n_thres):
        t_start = time.time()
        ftr = gen.ClassifierSampleFilter(c, thres_range[i])
        ftnet = gen.StochasticGenerativeNetWithFilter(model, ftr)
        s = ftnet.generate_samples(n_samples=data.shape[0]).asarray()
        acc_ftr, c_ftr = linear_classifier_discrimination(None, data, verbose=False, samples=s)
        print 'thres=%.2f, acc=%.4f, time=%.2f' % (thres_range[i], acc_ftr, time.time() - t_start)
        acc_rec[i] = acc_ftr
        if acc_ftr < acc_best:
            acc_best = acc_ftr
            best_ftnet = ftnet

    return best_ftnet

def get_filtered_model(net, data):
    acc, lr = linear_classifier_discrimination(net, data, C_range=[1, 10, 100, 1000], verbose=True)
    filter = gen.ClassifierSampleFilter(lr, threshold=0.8)
    filtered_net = gen.StochasticGenerativeNetWithFilter(net, filter)
    return filtered_net, lr

def test_single_filter_old(net, data, base_samples, base_classifier, threshold, base_filter=None):
    """
    net: the base net
    data: training data
    base_samples: samples generated by the base model with the base filter
    base_classifier: classifier trained to discriminate data from base_samples
    threshold: threshold used for the filter
    """
    if base_classifier is None:
        ftr = gen.BlankSampleFilter()
    else:
        ftr = gen.ClassifierSampleFilter(base_classifier, threshold, prev=base_filter)
    s = ftr.filter(base_samples)

    n_base = base_samples.shape[0]
    n_s = s.shape[0]

    print 'Filtered out %d/%d ~ %%%.1f' % (n_base - n_s, n_base, 100.0 * (n_base - n_s) / n_base)

    ftnet = gen.StochasticGenerativeNetWithFilter(net, ftr)
    ss = ftnet.generate_samples(n_samples=data.shape[0]).asarray()

    acc, c = linear_classifier_discrimination(net, data, samples=ss)

    return ftr, c

def test_single_filter(net, data, threshold, base_samples=None, base_classifier=None, base_filter=None):
    pass

def log_exp_sum_1d(x):
    """
    This computes log(exp(x_1) + exp(x_2) + ... + exp(x_n)) as 
    x* + log(exp(x_1-x*) + exp(x_2-x*) + ... + exp(x_n-x*)), where x* is the
    max over all x_i.  This can avoid numerical problems.
    """
    x_max = x.max()
    if isinstance(x, gnp.garray):
        return x_max + gnp.log(gnp.exp(x - x_max).sum())
    else:
        return x_max + np.log(np.exp(x - x_max).sum())

def log_exp_sum(x, axis=1):
    x_max = x.max(axis=axis)
    if isinstance(x, gnp.garray):
        return (x_max + gnp.log(gnp.exp(x - x_max[:,gnp.newaxis]).sum(axis=axis))).asarray()
    else:
        return x_max + np.log(np.exp(x - x_max[:,np.newaxis]).sum(axis=axis))

class KDE(object):
    """
    Kernel density estimation.
    """
    def __init__(self, data, sigma):
        self.x = gnp.garray(data) if not isinstance(data, gnp.garray) else data
        self.sigma = sigma
        self.N = self.x.shape[0]
        self.d = self.x.shape[1]
        self._ek =  ker.EuclideanKernel()

        self.factor = float(-np.log(self.N) - self.d / 2.0 * np.log(2 * np.pi * self.sigma**2))

    def _log_likelihood(self, data):
        return log_exp_sum(-self._ek.compute_kernel_transformation(self.x, data) / (2 * self.sigma**2), axis=1) + self.factor

    def log_likelihood(self, data, batch_size=1000):
        n_cases = data.shape[0]
        if n_cases <= batch_size:
            return self._log_likelihood(data)
        else:
            n_batches = (n_cases + batch_size - 1) / batch_size
            log_like = np.zeros(n_cases, dtype=np.float)

            for i_batch in range(n_batches):
                i_start = i_batch * batch_size
                i_end = n_cases if (i_batch + 1 == n_batches) else (i_start + batch_size)
                log_like[i_start:i_end] = self._log_likelihood(data[i_start:i_end])

            return log_like

    def likelihood(self, data):
        """
        data is a n_example x n_dims matrix.
        """
        return np.exp(self.log_likelihood(data))

    def average_likelihood(self, data):
        return self.likelihood(data).mean()

    def average_log_likelihood(self, data, batch_size=1000):
        return self.log_likelihood(data, batch_size=batch_size).mean()

    def average_std_log_likelihood(self, data, batch_size=1000):
        l = self.log_likelihood(data)
        return l.mean(), l.std()

    def average_se_log_likelihood(self, data, batch_size=1000):
        l = self.log_likelihood(data)
        return l.mean(), l.std() / np.sqrt(data.shape[0])

class AlternativeKDE(object):
    """
    Kernel density estimation.
    """
    def __init__(self, data, sigma):
        self.x = data if not isinstance(data, gnp.garray) else data.asarray()
        self.sigma = sigma
        self.N = self.x.shape[0]
        self.d = self.x.shape[1]

    def _compute_log_prob(self, data, batch_size=1000):
        """
        Break down data into smaller pieces so large matrix will also work.
        """
        data = data if not isinstance(data, gnp.garray) else data.asarray()
        n_cases = data.shape[0]
        K = np.zeros((n_cases, self.N), dtype=np.float)
        log_prob = np.zeros(n_cases, dtype=np.float)
        for i in range(n_cases):
            K[i] = -((self.x - data[i])**2).sum(axis=1) / (2 * self.sigma**2)
            log_prob[i] = log_exp_sum_1d(K[i]) - np.log(self.N) - self.d / 2.0 * (np.log(2 * np.pi) + 2 * np.log(self.sigma))

        return log_prob

    def likelihood(self, data):
        """
        data is a n_example x n_dims matrix.
        """
        return np.exp(self._compute_log_prob(data))

    def average_likelihood(self, data):
        return self.likelihood(data).mean()

    def log_likelihood(self, data):
        # return np.log(self._compute_kde(data) + 1e-50)# - self.d / 2.0 * (np.log(2 * np.pi) + 2 * np.log(self.sigma))
        return self._compute_log_prob(data)

    def average_log_likelihood(self, data):
        return self.log_likelihood(data).mean()


def kde_evaluation(test_data, samples, sigma_range=np.arange(0.1, 0.3, 0.01), verbose=True):
    best_log_likelihood = float('-inf')
    for sigma in sigma_range:
        log_likelihood = KDE(samples, sigma).average_log_likelihood(test_data)
        if log_likelihood > best_log_likelihood:
            best_log_likelihood = log_likelihood
        if verbose:
            print 'sigma=%g, log_likelihood=%.2f' % (sigma, log_likelihood)

    if verbose:
        print '===================='
        print 'Best log_likelihood=%.2f' % best_log_likelihood
        print ''
    return best_log_likelihood

def kde_evaluation_tfd(test_data, samples, sigma_range=np.arange(0.05, 0.25, 0.01), verbose=True):
    return kde_evaluation(test_data, samples, sigma_range, verbose)

def kde_evaluation_all_folds(test_data, samples, sigma_range=np.arange(0.05, 0.25, 0.01), verbose=True):
    n_folds = len(samples)
    best_log_likelihood = float('-inf')
    for sigma in sigma_range:
        log_likelihood = [KDE(samples[i], sigma).average_log_likelihood(test_data[i]) for i in range(n_folds)]
        avg_log_likelihood = sum(log_likelihood) / float(n_folds)
        if avg_log_likelihood > best_log_likelihood:
            best_log_likelihood = avg_log_likelihood
        if verbose:
            print 'sigma=%5g, log_likelihood=%8.2f   [%s]' % (sigma, avg_log_likelihood, ', '.join(['%8.2f' % l for l in log_likelihood]))

    if verbose:
        print '===================='
        print 'Best log_likelihood=%.2f' % best_log_likelihood
        print ''
    return best_log_likelihood

def generate_fold_samples(net, fold_model_format, ae=None, fold_ae_format=None, n_samples=10000, n_folds=5):
    samples = []
    for fold in range(n_folds):
        net.load_model_from_file(fold_model_format % fold)
        if ae is not None:
            ae.load_model_from_file(fold_ae_format % fold)
            net.autoencoder = ae
        samples.append(net.generate_samples(n_samples=n_samples))

    return samples

def get_fold_data(set_name, n_folds=5):
    data = []
    for i_fold in range(n_folds):
        x_train, x_val, x_test = load_tfd_fold(i_fold)
        if set_name == 'train':
            data.append(x_train)
        elif set_name == 'val':
            data.append(x_val)
        elif set_name == 'test':
            data.append(x_test)
    return data

def kde_eval_mnist(net, test_data, n_samples=10000, sigma_range=np.arange(0.1, 0.3, 0.01), verbose=True):
    s = net.generate_samples(n_samples=n_samples)
    best_log_likelihood = float('-inf')
    best_se = 0
    best_sigma = 0
    for sigma in sigma_range:
        log_likelihood, se = KDE(s, sigma).average_se_log_likelihood(test_data)
        if log_likelihood > best_log_likelihood:
            best_log_likelihood = log_likelihood
            best_se = se 
            best_sigma = sigma
        if verbose:
            print 'sigma=%g, log_likelihood=%.2f (%.2f)' % (sigma, log_likelihood, se)

    if verbose:
        print '===================='
        print 'Best log_likelihood=%.2f (%.2f)' % (best_log_likelihood, best_se)
        print ''
    return best_log_likelihood, best_se, best_sigma

def kde_eval_tfd(net, test_data_all_folds, n_samples=10000, sigma_range=np.arange(0.05, 0.25, 0.01), verbose=True):
    s = net.generate_samples(n_samples=n_samples)
    best_log_likelihood = float('-inf')
    n_folds = len(test_data_all_folds)
    for sigma in sigma_range:
        kde = KDE(s, sigma)
        log_likelihood = [kde.average_log_likelihood(test_data_all_folds[i]) for i in range(n_folds)]
        avg_log_likelihood = sum(log_likelihood) / float(n_folds)
        avg_se = np.array(log_likelihood).std() / np.sqrt(n_folds)
        if avg_log_likelihood > best_log_likelihood:
            best_log_likelihood = avg_log_likelihood
            best_se = avg_se
            best_sigma = sigma
        if verbose:
            print 'sigma=%5g, log_likelihood=%8.2f (%.2f)  [%s]' % (sigma, avg_log_likelihood, avg_se, ', '.join(['%8.2f' % l for l in log_likelihood]))

    if verbose:
        print '===================='
        print 'Best log_likelihood=%.2f (%.2f)' % (best_log_likelihood, best_se)
        print ''
    return best_log_likelihood, best_se, best_sigma


