from spikeinterface.core.core_tools import define_function_from_class

from .neobaseextractor import NeoBaseRecordingExtractor, NeoBaseSortingExtractor


class SpikeGadgetsRecordingExtractor(NeoBaseRecordingExtractor):
    """
    Class for reading *rec files from spikegadgets.

    Based on :py:class:`neo.rawio.SpikeGadgetsRawIO`

    Parameters
    ----------
    file_path: str
        The smr or smrx  file.
    stream_id: str or None
        If several stream, specify the one you want.
    all_annotations: bool  (default False)
        Load exhaustively all annotation from neo.
    """
    mode = 'file'
    NeoRawIOClass = 'SpikeGadgetsRawIO'

    def __init__(self, file_path, stream_id=None, all_annotations=False):
        neo_kwargs = {'filename': str(file_path)}
        NeoBaseRecordingExtractor.__init__(self, stream_id=stream_id, all_annotations=all_annotations, **neo_kwargs)
        self._kwargs.update(dict(file_path=str(file_path), stream_id=stream_id))


read_spikegadgets = define_function_from_class(source_class=SpikeGadgetsRecordingExtractor, name="read_spikegadgets")
