"""Sorting components: peak detection."""

import numpy as np

from spikeinterface.core.job_tools import (ChunkRecordingExecutor, _shared_job_kwargs_doc,
                                           split_job_kwargs, fix_job_kwargs)
from spikeinterface.core.recording_tools import get_noise_levels, get_channel_distances

from ..core import get_chunk_with_margin

from .peak_pipeline import PipelineNode, check_graph, run_nodes
from .tools import make_multi_method_doc

try:
    import numba
    HAVE_NUMBA = True
except ImportError:
    HAVE_NUMBA = False

base_peak_dtype = [('sample_ind', 'int64'), ('channel_ind', 'int64'),
                   ('amplitude', 'float64'), ('segment_ind', 'int64')]


def detect_peaks(recording, method='by_channel', pipeline_nodes=None, **kwargs):
    """Peak detection based on threshold crossing in term of k x MAD.

    In 'by_channel' : peak are detected in each channel independently
    In 'locally_exclusive' : a single best peak is taken from a set of neighboring channels


    Parameters
    ----------
    recording: RecordingExtractor
        The recording extractor object.
    pipeline_nodes: None or list[PipelineNode]
        Optional additional PipelineNode need to computed just after detection time.
        This avoid reading the recording multiple times.
    {method_doc}
    {job_doc}

    Returns
    -------
    peaks: array
        Detected peaks.

    Notes
    -----
    This peak detection ported from tridesclous into spikeinterface.
    """

    assert method in detect_peak_methods

    method_class = detect_peak_methods[method]
    
    method_kwargs, job_kwargs = split_job_kwargs(kwargs)

    # prepare args
    method_args = method_class.check_params(recording, **method_kwargs)

    extra_margin = 0
    if pipeline_nodes is not None:
        check_graph(pipeline_nodes)
        extra_margin = max(node.get_trace_margin() for node in pipeline_nodes)

    func = _detect_peaks_chunk
    init_func = _init_worker_detect_peaks
    init_args = (recording, method, method_args, extra_margin, pipeline_nodes)
    processor = ChunkRecordingExecutor(recording, func, init_func, init_args,
                                       handle_returns=True, job_name='detect peaks',
                                       **job_kwargs)
    outputs = processor.run()

    if pipeline_nodes is None:
        peaks = np.concatenate(outputs)
        return peaks
    else:

        outs_concat = ()
        for output_node in zip(*outputs):
            outs_concat += (np.concatenate(output_node, axis=0), )
        return outs_concat


def _init_worker_detect_peaks(recording, method, method_args, extra_margin, pipeline_nodes):
    """Initialize a worker for detecting peaks."""

    if isinstance(recording, dict):
        from spikeinterface.core import load_extractor
        recording = load_extractor(recording)

        if pipeline_nodes is not None:
            pipeline_nodes = [cls.from_dict(recording, kwargs) for cls, kwargs in pipeline_nodes]

    # create a local dict per worker
    worker_ctx = {}
    worker_ctx['recording'] = recording
    worker_ctx['method'] = method
    worker_ctx['method_class'] = detect_peak_methods[method]
    # import copy
    # worker_ctx['method_args'] = copy.deepcopy(method_args)
    worker_ctx['method_args'] = method_args
    worker_ctx['extra_margin'] = extra_margin
    worker_ctx['pipeline_nodes'] = pipeline_nodes
    
    return worker_ctx


def _detect_peaks_chunk(segment_index, start_frame, end_frame, worker_ctx):

    # recover variables of the worker
    recording = worker_ctx['recording']
    method_class = worker_ctx['method_class']
    method_args = worker_ctx['method_args']
    extra_margin = worker_ctx['extra_margin']
    pipeline_nodes = worker_ctx['pipeline_nodes']

    margin = method_class.get_method_margin(*method_args) + extra_margin

    # load trace in memory
    recording_segment = recording._recording_segments[segment_index]
    traces, left_margin, right_margin = get_chunk_with_margin(recording_segment, start_frame, end_frame,
                                                              None, margin, add_zeros=True)

    if extra_margin > 0:
        # remove extra margin for detection node
        trace_detection = traces[extra_margin:-extra_margin]
    else:
        trace_detection = traces

    
    # import os
    # print('_detect_peaks_chunk', os.getpid(), method_args[0].x, method_args[0].ctx, )


    peak_sample_ind, peak_chan_ind = method_class.detect_peaks(trace_detection, *method_args)

    if extra_margin > 0:
        peak_sample_ind += extra_margin

    peak_dtype = base_peak_dtype
    peak_amplitude = traces[peak_sample_ind, peak_chan_ind]

    peaks = np.zeros(peak_sample_ind.size, dtype=peak_dtype)
    peaks['sample_ind'] = peak_sample_ind
    peaks['channel_ind'] = peak_chan_ind
    peaks['amplitude'] = peak_amplitude
    peaks['segment_ind'] = segment_index

    if pipeline_nodes is not None:
        outs = run_nodes(traces, peaks, pipeline_nodes)

    # make absolute sample index
    peaks['sample_ind'] += (start_frame - left_margin)

    if pipeline_nodes is None:
        return peaks
    else:
        return (peaks, ) + outs


class DetectPeakByChannel:
    """Detect peaks using the 'by channel' method.
    """

    name = 'by_channel'
    engine = 'numpy'
    params_doc = """
    peak_sign: 'neg', 'pos', 'both'
        Sign of the peak.
    detect_threshold: float
        Threshold, in median absolute deviations (MAD), to use to detect peaks.
    exclude_sweep_ms: float or None
        Time, in ms, during which the peak is isolated. Exclusive param with exclude_sweep_size
        For example, if `exclude_sweep_ms` is 0.1, a peak is detected if a sample crosses the threshold,
        and no larger peaks are located during the 0.1ms preceding and following the peak.
    noise_levels: array, optional
        Estimated noise levels to use, if already computed.
        If not provide then it is estimated from a random snippet of the data.
    random_chunk_kwargs: dict, optional
        A dict that contain option to randomize chunk for get_noise_levels().
        Only used if noise_levels is None."""

    @classmethod
    def check_params(cls, recording, peak_sign='neg', detect_threshold=5,
                     exclude_sweep_ms=0.1, noise_levels=None, random_chunk_kwargs={}):
        
        assert peak_sign in ('both', 'neg', 'pos')

        if noise_levels is None:
            noise_levels = get_noise_levels(recording, return_scaled=False, **random_chunk_kwargs)
        abs_threholds = noise_levels * detect_threshold
        exclude_sweep_size = int(exclude_sweep_ms * recording.get_sampling_frequency() / 1000.)

        return (peak_sign, abs_threholds, exclude_sweep_size)
    
    @classmethod
    def get_method_margin(cls, *args):
        exclude_sweep_size = args[2]
        return exclude_sweep_size

    @classmethod
    def detect_peaks(cls, traces, peak_sign, abs_threholds, exclude_sweep_size):
        traces_center = traces[exclude_sweep_size:-exclude_sweep_size, :]
        length = traces_center.shape[0]

        if peak_sign in ('pos', 'both'):
            peak_mask = traces_center > abs_threholds[None, :]
            for i in range(exclude_sweep_size):
                peak_mask &= traces_center > traces[i:i + length, :]
                peak_mask &= traces_center >= traces[exclude_sweep_size +
                                                    i + 1:exclude_sweep_size + i + 1 + length, :]

        if peak_sign in ('neg', 'both'):
            if peak_sign == 'both':
                peak_mask_pos = peak_mask.copy()

            peak_mask = traces_center < -abs_threholds[None, :]
            for i in range(exclude_sweep_size):
                peak_mask &= traces_center < traces[i:i + length, :]
                peak_mask &= traces_center <= traces[exclude_sweep_size +
                                                    i + 1:exclude_sweep_size + i + 1 + length, :]

            if peak_sign == 'both':
                peak_mask = peak_mask | peak_mask_pos

        # find peaks
        peak_sample_ind, peak_chan_ind = np.nonzero(peak_mask)
        # correct for time shift
        peak_sample_ind += exclude_sweep_size

        return peak_sample_ind, peak_chan_ind


class DetectPeakLocallyExclusive:
    """Detect peaks using the 'locally exclusive' method."""

    name = 'locally_exclusive'
    engine = 'numba'
    params_doc = DetectPeakByChannel.params_doc + """
    local_radius_um: float
        The radius to use to select neighbour channels for locally exclusive detection.
    """
    @classmethod
    def check_params(cls, recording, peak_sign='neg', detect_threshold=5,
                     exclude_sweep_ms=0.1, local_radius_um=50, noise_levels=None, random_chunk_kwargs={}):

        if not HAVE_NUMBA:
            raise ModuleNotFoundError('"locally_exclusive" need numba which is not installed')

        args = DetectPeakByChannel.check_params(recording, peak_sign=peak_sign, detect_threshold=detect_threshold,
                     exclude_sweep_ms=exclude_sweep_ms, noise_levels=noise_levels, random_chunk_kwargs=random_chunk_kwargs)

        channel_distance = get_channel_distances(recording)
        neighbours_mask = channel_distance < local_radius_um
        return args + (neighbours_mask, )

    @classmethod
    def get_method_margin(cls, *args):
        exclude_sweep_size = args[2]
        return exclude_sweep_size

    @classmethod
    def detect_peaks(cls, traces, peak_sign, abs_threholds, exclude_sweep_size, neighbours_mask):
        assert HAVE_NUMBA, 'You need to install numba'
        traces_center = traces[exclude_sweep_size:-exclude_sweep_size, :]

        if peak_sign in ('pos', 'both'):
            peak_mask = traces_center > abs_threholds[None, :]
            peak_mask = _numba_detect_peak_pos(traces, traces_center, peak_mask, exclude_sweep_size,
                                            abs_threholds, peak_sign, neighbours_mask)

        if peak_sign in ('neg', 'both'):
            if peak_sign == 'both':
                peak_mask_pos = peak_mask.copy()

            peak_mask = traces_center < -abs_threholds[None, :]
            peak_mask = _numba_detect_peak_neg(traces, traces_center, peak_mask, exclude_sweep_size,
                                            abs_threholds, peak_sign, neighbours_mask)

            if peak_sign == 'both':
                peak_mask = peak_mask | peak_mask_pos

        # Find peaks and correct for time shift
        peak_sample_ind, peak_chan_ind = np.nonzero(peak_mask)
        peak_sample_ind += exclude_sweep_size

        return peak_sample_ind, peak_chan_ind

if HAVE_NUMBA:
    @numba.jit(parallel=False)
    def _numba_detect_peak_pos(traces, traces_center, peak_mask, exclude_sweep_size,
                               abs_threholds, peak_sign, neighbours_mask):
        num_chans = traces_center.shape[1]
        for chan_ind in range(num_chans):
            for s in range(peak_mask.shape[0]):
                if not peak_mask[s, chan_ind]:
                    continue
                for neighbour in range(num_chans):
                    if not neighbours_mask[chan_ind, neighbour]:
                        continue
                    for i in range(exclude_sweep_size):
                        if chan_ind != neighbour:
                            peak_mask[s, chan_ind] &= traces_center[s, chan_ind] >= traces_center[s, neighbour]
                        peak_mask[s, chan_ind] &= traces_center[s, chan_ind] > traces[s + i, neighbour]
                        peak_mask[s, chan_ind] &= traces_center[s, chan_ind] >= traces[exclude_sweep_size + s + i + 1, neighbour]
                        if not peak_mask[s, chan_ind]:
                            break
                    if not peak_mask[s, chan_ind]:
                        break
        return peak_mask

    @numba.jit(parallel=False)
    def _numba_detect_peak_neg(traces, traces_center, peak_mask, exclude_sweep_size,
                               abs_threholds, peak_sign, neighbours_mask):
        num_chans = traces_center.shape[1]
        for chan_ind in range(num_chans):
            for s in range(peak_mask.shape[0]):
                if not peak_mask[s, chan_ind]:
                    continue
                for neighbour in range(num_chans):
                    if not neighbours_mask[chan_ind, neighbour]:
                        continue
                    for i in range(exclude_sweep_size):
                        if chan_ind != neighbour:
                            peak_mask[s, chan_ind] &= traces_center[s, chan_ind] <= traces_center[s, neighbour]
                        peak_mask[s, chan_ind] &= traces_center[s, chan_ind] < traces[s + i, neighbour]
                        peak_mask[s, chan_ind] &= traces_center[s, chan_ind] <= traces[exclude_sweep_size + s + i + 1, neighbour]
                        if not peak_mask[s, chan_ind]:
                            break
                    if not peak_mask[s, chan_ind]:
                        break
        return peak_mask


class DetectPeakLocallyExclusiveOpenCL:
    name = 'locally_exclusive_cl'
    engine = 'opencl'
    preferred_mp_context = None
    params_doc = DetectPeakLocallyExclusive.params_doc + """
    opencl_context_kwargs: None or dict
        kwargs to create the opencl context
    """
    @classmethod
    def check_params(cls, recording, peak_sign='neg', detect_threshold=5,
                     exclude_sweep_ms=0.1, local_radius_um=50, noise_levels=None, random_chunk_kwargs={}):
        
        # TODO refactor with other classes
        assert peak_sign in ('both', 'neg', 'pos')
        if noise_levels is None:
            noise_levels = get_noise_levels(recording, return_scaled=False, **random_chunk_kwargs)
        abs_threholds = noise_levels * detect_threshold
        exclude_sweep_size = int(exclude_sweep_ms * recording.get_sampling_frequency() / 1000.)
        channel_distance = get_channel_distances(recording)
        neighbours_mask = channel_distance < local_radius_um
        
        executor = OpenCLDetectPeakExecutor(abs_threholds, exclude_sweep_size, neighbours_mask, peak_sign)
        
        return (executor, )

    @classmethod
    def get_method_margin(cls, *args):
        executor = args[0]
        return executor.exclude_sweep_size

    @classmethod
    def detect_peaks(cls, traces, executor):
        peak_sample_ind, peak_chan_ind = executor.detect_peak(traces)
        
        return peak_sample_ind, peak_chan_ind


class OpenCLDetectPeakExecutor:
    def __init__(self, abs_threholds, exclude_sweep_size, neighbours_mask, peak_sign):
        import pyopencl
        self.chunk_size = None
        
        self.abs_threholds = abs_threholds.astype('float32')
        self.exclude_sweep_size = exclude_sweep_size
        self.neighbours_mask = neighbours_mask.astype('uint8')
        self.peak_sign = peak_sign
        self.ctx = None
        self.queue = None
        self.x = 0
    
    def create_buffers_and_compile(self, chunk_size):
        import pyopencl
        mf = pyopencl.mem_flags
        try:
            self.device = pyopencl.get_platforms()[0].get_devices()[0]
            self.ctx = pyopencl.Context(devices=[self.device])
        except Exception as e:
            print('error create context ', e)

        self.queue = pyopencl.CommandQueue(self.ctx)
        self.max_wg_size = self.ctx.devices[0].get_info(pyopencl.device_info.MAX_WORK_GROUP_SIZE)
        self.chunk_size = chunk_size

        self.neighbours_mask_cl = pyopencl.Buffer(self.ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=self.neighbours_mask)
        self.abs_threholds_cl = pyopencl.Buffer(self.ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=self.abs_threholds)

        num_channels = self.neighbours_mask.shape[0]
        self.traces_cl = pyopencl.Buffer(self.ctx, mf.READ_WRITE, size=int(chunk_size * num_channels * 4))

        # TODO estimate smaller 
        self.num_peaks = np.zeros(1, dtype='int32')
        self.num_peaks_cl = pyopencl.Buffer(self.ctx, mf.READ_WRITE| mf.COPY_HOST_PTR, hostbuf=self.num_peaks)

        nb_max_spike_in_chunk = num_channels * chunk_size
        self.peaks = np.zeros(nb_max_spike_in_chunk, dtype=[('sample_index', 'int32'), ('channel_index', 'int32')])
        self.peaks_cl = pyopencl.Buffer(self.ctx, mf.READ_WRITE| mf.COPY_HOST_PTR, hostbuf=self.peaks)

        variables = dict(
            chunk_size=int(self.chunk_size),
            exclude_sweep_size=int(self.exclude_sweep_size),
            peak_sign={'pos': 1, 'neg': -1}[self.peak_sign],
            num_channels=num_channels,
        )

        kernel_formated = processor_kernel % variables
        prg = pyopencl.Program(self.ctx, kernel_formated)
        self.opencl_prg = prg.build()  # options='-cl-mad-enable'
        self.kern_detect_peaks = getattr(self.opencl_prg, 'detect_peaks')

        self.kern_detect_peaks.set_args(self.traces_cl,
                                        self.neighbours_mask_cl,
                                        self.abs_threholds_cl,
                                        self.peaks_cl,
                                        self.num_peaks_cl)

        s = self.chunk_size - 2 * self.exclude_sweep_size
        self.global_size = (s, )
        self.local_size = None


    def detect_peak(self, traces):
        self.x += 1

        import pyopencl
        if self.chunk_size is None or self.chunk_size != traces.shape[0]:
            self.create_buffers_and_compile(traces.shape[0])
        event = pyopencl.enqueue_copy(self.queue,  self.traces_cl, traces.astype('float32'))

        pyopencl.enqueue_nd_range_kernel(self.queue,  self.kern_detect_peaks, self.global_size, self.local_size,)

        event = pyopencl.enqueue_copy(self.queue,  self.traces_cl, traces.astype('float32'))
        event = pyopencl.enqueue_copy(self.queue,  self.traces_cl, traces.astype('float32'))
        event = pyopencl.enqueue_copy(self.queue,  self.num_peaks,self.num_peaks_cl)
        event = pyopencl.enqueue_copy(self.queue,  self.peaks, self.peaks_cl)
        event.wait()

        n = self.num_peaks[0]
        peaks = self.peaks[:n]
        peak_sample_ind = peaks['sample_index'].astype('int64')
        peak_chan_ind = peaks['channel_index'].astype('int64')

        return peak_sample_ind, peak_chan_ind


processor_kernel = """
#define chunk_size %(chunk_size)d
#define exclude_sweep_size %(exclude_sweep_size)d
#define peak_sign %(peak_sign)d
#define num_channels %(num_channels)d


typedef struct st_peak{
    int sample_index;
    int channel_index;
} st_peak;


__kernel void detect_peaks(
                        //in
                        __global  float *traces,
                        __global  uchar *neighbours_mask,
                        __global  float *abs_threholds,
                        //out
                        __global  st_peak *peaks,
                        volatile __global int *num_peaks
                ){
    int pos = get_global_id(0);
    
    if (pos == 0){
        *num_peaks = 0;
    }
    // this barrier OK if the first group is run first
    barrier(CLK_GLOBAL_MEM_FENCE);

    if (pos>=(chunk_size - (2 * exclude_sweep_size))){
        return;
    }
    

    float v;
    uchar peak;
    uchar is_neighbour;
    
    int index;
    
    int i_peak;

    
    for (int chan=0; chan<num_channels; chan++){
        
        //v = traces[(pos + exclude_sweep_size)*num_channels + chan];
        index = (pos + exclude_sweep_size) * num_channels + chan;
        v = traces[index];
        
        if(peak_sign==1){
            if (v>abs_threholds[chan]){peak=1;}
            else {peak=0;}
        }
        else if(peak_sign==-1){
            if (v<-abs_threholds[chan]){peak=1;}
            else {peak=0;}
        }
        
        if (peak == 1){
            for (int chan_neigh=0; chan_neigh<num_channels; chan_neigh++){
            
                is_neighbour = neighbours_mask[chan * num_channels + chan_neigh];
                if (is_neighbour == 0){continue;}
                //if (chan == chan_neigh){continue;}

                index = (pos + exclude_sweep_size) * num_channels + chan_neigh;
                if(peak_sign==1){
                    peak = peak && (v>=traces[index]);
                }
                else if(peak_sign==-1){
                    peak = peak && (v<=traces[index]);
                }
                
                if (peak==0){break;}
                
                if(peak_sign==1){
                    for (int i=1; i<=exclude_sweep_size; i++){
                        peak = peak && (v>traces[(pos + exclude_sweep_size - i)*num_channels + chan_neigh]) && (v>=traces[(pos + exclude_sweep_size + i)*num_channels + chan_neigh]);
                        if (peak==0){break;}
                    }
                }
                else if(peak_sign==-1){
                    for (int i=1; i<=exclude_sweep_size; i++){
                        peak = peak && (v<traces[(pos + exclude_sweep_size - i)*num_channels + chan_neigh]) && (v<=traces[(pos + exclude_sweep_size + i)*num_channels + chan_neigh]);
                        if (peak==0){break;}
                    }
                }

            }

        }
        
        if (peak==1){
            //append to 
            i_peak = atomic_inc(num_peaks);
            // sample_index is LOCAL to fifo
            peaks[i_peak].sample_index = pos + exclude_sweep_size;
            peaks[i_peak].channel_index = chan;
        }
    }
    
}
"""


# TODO make a dict with name+engine entry later
_methods_list = [DetectPeakByChannel, DetectPeakLocallyExclusive,
                 DetectPeakLocallyExclusiveOpenCL]
detect_peak_methods = {m.name: m for m in _methods_list}
method_doc = make_multi_method_doc(_methods_list)
detect_peaks.__doc__ = detect_peaks.__doc__.format(method_doc=method_doc,
                                                   job_doc=_shared_job_kwargs_doc)

