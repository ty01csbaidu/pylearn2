from pylearn2.monitor import Monitor
from pylearn2.space import VectorSpace
from pylearn2.models.model import Model
from pylearn2.datasets.dense_design_matrix import DenseDesignMatrix
from pylearn2.training_algorithms.default import DefaultTrainingAlgorithm
import numpy as np
from theano import tensor as T
from pylearn2.models.s3c import S3C, E_Step, Grad_M_Step
from nose.plugins.skip import SkipTest
from pylearn2.utils import sharedX
from pylearn2.utils.serial import to_string
from pylearn2.utils.serial import from_string
from pylearn2.utils.iteration import _iteration_schemes
from theano import shared


class DummyModel(Model):
    def  __init__(self, num_features):
        self.input_space = VectorSpace(num_features)

class DummyDataset(DenseDesignMatrix):
    def __init__(self, num_examples, num_features):
        rng = np.random.RandomState([4, 12, 17])
        super(DummyDataset, self).__init__(
            X=rng.uniform(1., 2., (num_examples, num_features))
        )

class UnserializableDataset(DummyDataset):
    """ A Dataset that raises an assertion if you try to seralize it.
        Used to test that the monitor does not serialized its dataset.
        Since the Model seralizes its Monitor, this would cause a copy
        of the dataset to get saved with ever saved Model!"""

    def __getstate__(self):
        raise AssertionError("Dataset should not be serialized")

class ArangeDataset(DenseDesignMatrix):
    """ A dataset where example i is just the number i.
    Makes it easy to track which sets of examples are visited."""
    def __init__(self, num_examples):
        X = np.zeros((num_examples,1))
        X[:,0] = np.arange(num_examples)
        super(ArangeDataset, self).__init__(X)

def test_channel_scaling_sequential():
    def channel_scaling_checker(num_examples, mode, num_batches, batch_size):
        num_features = 2
        monitor = Monitor(DummyModel(num_features))
        dataset = DummyDataset(num_examples, num_features)
        try:
            monitor.add_dataset(dataset=dataset, mode=mode,
                                num_batches=num_batches, batch_size=batch_size)
        except NotImplementedError:
            # make sure this was due to the unimplemented batch_size case
            if num_batches is None:
                assert num_examples % batch_size != 0
            else:
                assert num_examples % num_batches != 0
            raise SkipTest()
        vis_batch = T.matrix()
        mean = vis_batch.mean()
        monitor.add_channel(name='mean', ipt=vis_batch, val=mean, dataset=dataset)
        monitor()
        assert 'mean' in monitor.channels
        mean = monitor.channels['mean']
        assert len(mean.val_record) == 1
        actual = mean.val_record[0]
        X = dataset.get_design_matrix()
        if batch_size is not None and num_batches is not None:
            total = min(num_examples, num_batches * batch_size)
        else:
            total = num_examples
        expected = X[:total].mean()
        if not np.allclose(expected, actual):
            raise AssertionError("Expected monitor to contain %f but it has "
                                 "%f" % (expected, actual))

    # Specifying num_batches; even split
    yield channel_scaling_checker, 10, 'sequential', 5, None
    # Specifying num_batches; even split
    yield channel_scaling_checker, 10, 'sequential', 2, None
    # Specifying batch_size; even split
    yield channel_scaling_checker, 10, 'sequential', None, 5
    # Specifying batch_size; even split
    yield channel_scaling_checker, 10, 'sequential', None, 2
    # Specifying num_batches; uneven split
    yield channel_scaling_checker, 10, 'sequential', 4, None
    # Specifying num_batches; uneven split
    yield channel_scaling_checker, 10, 'sequential', 3, None
    # Specifying batch_size; uneven split
    yield channel_scaling_checker, 10, 'sequential', None, 3
    # Specifying batch_size; uneven split
    yield channel_scaling_checker, 10, 'sequential', None, 4
    # Specifying both, even split
    yield channel_scaling_checker, 10, 'sequential', 2, 5
    # Specifying both, even split
    yield channel_scaling_checker, 10, 'sequential', 5, 2
    # Specifying both, uneven split, dangling batch
    yield channel_scaling_checker, 10, 'sequential', 3, 4
    # Specifying both, uneven split, non-exhaustive
    yield channel_scaling_checker, 10, 'sequential', 3, 3

def test_counting():
    BATCH_SIZE = 2
    BATCHES = 3
    NUM_FEATURES = 4
    num_examples = BATCHES * BATCH_SIZE
    dataset = DummyDataset( num_examples = num_examples,
            num_features = NUM_FEATURES)
    algorithm = DefaultTrainingAlgorithm( batch_size = BATCH_SIZE,
            batches_per_iter = BATCHES)
    model = S3C( nvis = NUM_FEATURES, nhid = 1,
            irange = .01, init_bias_hid = 0., init_B = 1.,
            min_B = 1., max_B = 1., init_alpha = 1.,
            min_alpha = 1., max_alpha = 1., init_mu = 0.,
            m_step = Grad_M_Step( learning_rate = 0.),
            e_step = E_Step( h_new_coeff_schedule = [ 1. ]))
    algorithm.setup(model = model, dataset = dataset)
    algorithm.train(dataset = dataset)
    if not ( model.monitor.get_batches_seen() == BATCHES):
        raise AssertionError('Should have seen '+str(BATCHES) + \
                ' batches but saw '+str(model.monitor.get_batches_seen()))

    assert model.monitor.get_examples_seen() == num_examples
    assert isinstance(model.monitor.get_examples_seen(),int)
    assert isinstance(model.monitor.get_batches_seen(),int)


def test_prereqs():

    # Test that prereqs get run before the monitoring channels are computed

    BATCH_SIZE = 2
    num_examples = BATCH_SIZE
    NUM_FEATURES = 3

    model = DummyModel(NUM_FEATURES)
    monitor = Monitor.get_monitor(model)

    monitoring_dataset = DummyDataset(num_examples = num_examples,
            num_features = NUM_FEATURES)

    monitor.add_dataset(monitoring_dataset, 'sequential', batch_size=BATCH_SIZE)

    prereq_counter = sharedX(0.)
    def prereq(X,y):
        prereq_counter.set_value(
                prereq_counter.get_value()+1.)

    name = 'num_prereq_calls'

    monitor.add_channel(name = name,
            ipt = model.input_space.make_theano_batch(),
            val = prereq_counter,
            prereqs = [ prereq ])

    channel = monitor.channels[name]

    assert len(channel.val_record) == 0
    monitor()
    assert channel.val_record == [1]
    monitor()
    assert channel.val_record == [1,2]

def test_revisit():

    # Test that each call to monitor revisits exactly the same data

    BATCH_SIZE = 3
    MAX_BATCH_SIZE = 12
    BATCH_SIZE_STRIDE = 3
    NUM_BATCHES = 10
    num_examples = NUM_BATCHES * BATCH_SIZE

    monitoring_dataset = ArangeDataset(num_examples)

    for mon_batch_size in xrange(BATCH_SIZE, MAX_BATCH_SIZE + 1,
            BATCH_SIZE_STRIDE):
        for num_mon_batches in [ 1, 3, num_examples / mon_batch_size, None ]:
            for mode in sorted(_iteration_schemes):

                if num_mon_batches is None and mode in ['random_uniform', 'random_slice']:
                    continue

                model = DummyModel(1)
                monitor = Monitor.get_monitor(model)

                try:
                    try:
                        monitor.add_dataset(monitoring_dataset, mode,
                            batch_size=mon_batch_size, num_batches=num_mon_batches)
                    except TypeError:
                        monitor.add_dataset(monitoring_dataset, mode,
                            batch_size=mon_batch_size, num_batches=num_mon_batches,
                            seed = 0)
                except NotImplementedError:
                    # Monitor does not currently support uneven iterators, so skip
                    # uneven iteration modes
                    # Check that this is what caused the error
                    if num_mon_batches is not None and mon_batch_size * num_mon_batches > num_examples:
                        continue
                    if num_mon_batches is None and num_examples % mon_batch_size != 0:
                        continue
                    print num_mon_batches, mon_batch_size, num_examples, mode
                    raise

                if num_mon_batches is None:
                    num_mon_batches = num_examples / mon_batch_size

                batches = [ None ] * num_mon_batches
                visited = [ False ] * num_mon_batches

                batch_idx = shared(0)

                class RecorderAndValidator:

                    def __init__(self):
                        self.validate = False

                    def __call__(self, X, y):
                        """ Initially, records the batches the monitor shows it.
                        When set to validate mode, makes sure the batches shown
                        on the second monitor call match those from the first."""
                        assert y is None

                        idx = batch_idx.get_value()
                        batch_idx.set_value(idx + 1)

                        # Note: if the monitor starts supporting variable batch sizes,
                        # take this out. Maybe move it to a new test that the iterator's
                        # uneven property is set accurately
                        assert X.shape[0] == mon_batch_size

                        if self.validate:
                            previous_batch = batches[idx]
                            assert not visited[idx]
                            visited[idx] = True
                            if not np.allclose(previous_batch, X):
                                print 'Visited different data in batch',idx
                                print previous_batch
                                print X
                                print 'Iteration mode', mode
                                assert False
                        else:
                            batches[idx] = X
                        # end if
                    # end __call__
                #end class

                prereq = RecorderAndValidator()

                monitor.add_channel(name = 'dummy',
                    ipt = model.input_space.make_theano_batch(),
                    val = 0.,
                    prereqs = [ prereq ])

                monitor()

                assert None not in batches

                batch_idx.set_value(0)
                prereq.validate = True

                monitor()

                assert all(visited)

def test_prereqs_batch():

    # Test that prereqs get run before each monitoring batch

    BATCH_SIZE = 2
    num_examples = 2 * BATCH_SIZE
    NUM_FEATURES = 3

    model = DummyModel(NUM_FEATURES)
    monitor = Monitor.get_monitor(model)

    monitoring_dataset = DummyDataset(num_examples = num_examples,
            num_features = NUM_FEATURES)

    monitor.add_dataset(monitoring_dataset, 'sequential', batch_size=BATCH_SIZE)

    sign = sharedX(1.)
    def prereq(X,y):
        sign.set_value(
                -sign.get_value())

    name = 'batches_should_cancel_to_0'

    monitor.add_channel(name = name,
            ipt = model.input_space.make_theano_batch(),
            val = sign,
            prereqs = [ prereq ])

    channel = monitor.channels[name]

    assert len(channel.val_record) == 0
    monitor()
    assert channel.val_record == [0]
    monitor()
    assert channel.val_record == [0,0]


def test_dont_serialize_dataset():

    # Test that serializing the monitor does not serialize the dataset

    BATCH_SIZE = 2
    num_examples = 2 * BATCH_SIZE
    NUM_FEATURES = 3

    model = DummyModel(NUM_FEATURES)
    monitor = Monitor.get_monitor(model)

    monitoring_dataset = UnserializableDataset(num_examples = num_examples,
            num_features = NUM_FEATURES)
    monitoring_dataset.yaml_src = ""

    monitor.add_dataset(monitoring_dataset, 'sequential', batch_size=BATCH_SIZE)

    to_string(monitor)

def test_serialize_twice():

    # Test that a monitor can be serialized twice
    # with the same result

    model = DummyModel(1)
    monitor = Monitor.get_monitor(model)

    x = to_string(monitor)
    y = to_string(monitor)

    assert x == y

def test_valid_after_serialize():

    # Test that serializing the monitor does not ruin it

    BATCH_SIZE = 2
    num_examples = 2 * BATCH_SIZE
    NUM_FEATURES = 3

    model = DummyModel(NUM_FEATURES)
    monitor = Monitor.get_monitor(model)

    monitoring_dataset = UnserializableDataset(num_examples = num_examples,
            num_features = NUM_FEATURES)
    monitoring_dataset.yaml_src = ""

    monitor.add_dataset(monitoring_dataset, 'sequential', batch_size=BATCH_SIZE)

    to_string(monitor)

    monitor.redo_theano()

def test_deserialize():

    # Test that a monitor can be deserialized

    model = DummyModel(1)
    monitor = Monitor.get_monitor(model)

    x = to_string(monitor)
    monitor = from_string(x)
    y = to_string(monitor)
