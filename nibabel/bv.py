# emacs: -*- mode: python-mode; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the NiBabel package for the
#   copyright and license terms.
#
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
''' Header reading / writing functions for Brainvoyager (BV) file formats

Author: Thomas Emmerling
'''

import numpy as np

from .volumeutils import allopen, array_to_file, array_from_file, Recoder
from .spatialimages import HeaderDataError, HeaderTypeError, ImageFileError, SpatialImage, Header
from .fileholders import FileHolder,  copy_file_map
from .arrayproxy import CArrayProxy
from .volumeutils import (shape_zoom_affine, apply_read_scaling, seek_tell, make_dt_codes,
                                 pretty_mapping, endian_codes, native_code, swapped_code)
from .arraywriters import make_array_writer, WriterError, get_slope_inter
from .wrapstruct import LabeledWrapStruct
from . import imageglobals as imageglobals
from .batteryrunners import Report, BatteryRunner

_dtdefs = ( # code, conversion function, equivalent dtype, aliases
    (1, 'short int', np.dtype(np.uint16).newbyteorder('<')),
    (2, 'float', np.dtype(np.float32).newbyteorder('<')))

# Make full code alias bank, including dtype column
data_type_codes = make_dt_codes(_dtdefs)

class BvError(Exception):
    """Exception for BV format related problems.

    To be raised whenever there is a problem with a BV fileformat.
    """
    pass

class BvFileHeader(LabeledWrapStruct):
    """Class to hold information from a BV file header.
    """

    # Copies of module-level definitions
    _data_type_codes = data_type_codes


    # data scaling capabilities
    has_data_slope = False
    has_data_intercept = False

    _field_recoders = {'datatype': data_type_codes}

    def __init__(self,
                 binaryblock=None,
                 endianness='<',
                 check=True):
        ''' Initialize header from binary data block

        Parameters
        ----------
        binaryblock : {None, string} optional
            binary block to set into header.  By default, None, in
            which case we insert the default empty header block
        endianness : {None, '<','>', other endian code} string, optional
            endianness of the binaryblock.  If None, guess endianness
            from the data.
        check : bool, optional
            Whether to check content of header in initialization.
            Default is True.
        '''

        if binaryblock is None:
            self._structarr = self.__class__.default_structarr()
            self.template_dtype = self.update_template_dtype(binaryblock)
            return

        self.template_dtype = self.update_template_dtype(binaryblock)

        wstr = np.ndarray(shape=(),
                         dtype=self.template_dtype,
                         buffer=binaryblock[:self.template_dtype.itemsize])
        self._structarr = wstr.copy()
        if check:
            self.check_fix()
        return

    def update_template_dtype(self, binaryblock=None, item=None, value=None):
        raise NotImplementedError

    @classmethod
    def from_fileobj(klass, fileobj, endianness=None, check=True):
        ''' Return read structure with given or guessed endiancode

        Parameters
        ----------
        fileobj : file-like object
           Needs to implement ``read`` method
        endianness : None or endian code, optional
           Code specifying endianness of read data

        Returns
        -------
        header : BvFileHeader object
           BvFileHeader object initialized from data in fileobj
        '''
        raw_str = fileobj.read(10000)
        return klass(raw_str, endianness, check)

    def __setitem__(self, item, value):
        ''' Set values in structured data
        check for string values and change the template accordingly
        '''
        if self.template_dtype[item].type == np.string_:
            self.update_template_dtype(item=item, value=value)
            wstr = np.ndarray(shape=(),
                             dtype=self.template_dtype,
                             buffer=np.zeros(self.template_dtype.itemsize))
            for key in self.keys():
                wstr[key] = self._structarr[key]
            self._structarr = wstr.copy()
        self._structarr[item] = value

    @classmethod
    def default_structarr(klass):
        raise NotImplementedError

    @classmethod
    def from_header(klass, header=None, check=False):
        ''' Class method to create header from another header

        Parameters
        ----------
        header : ``Header`` instance or mapping
           a header of this class, or another class of header for
           conversion to this type
        check : {True, False}
           whether to check header for integrity

        Returns
        -------
        hdr : header instance
           fresh header instance of our own class
        '''
        # own type, return copy
        if type(header) == klass:
            obj = header.copy()
            if check:
                obj.check_fix()
            return obj
        # not own type, make fresh header instance
        obj = klass(check=check)
        if header is None:
            return obj
        try: # check if there is a specific conversion routine
            mapping = header.as_bv_map()
        except AttributeError:
            # most basic conversion
            obj.set_data_dtype(header.get_data_dtype())
            obj.set_data_shape(header.get_data_shape())
            obj.set_zooms(header.get_zooms())
            return obj
        # header is convertible from a field mapping
        for key, value in mapping.items():
            try:
                obj[key] = value
            except (ValueError, KeyError):
                # the presence of the mapping certifies the fields as
                # being of the same meaning as for BV types
                pass
        # set any fields etc that are specific to this format (overriden by
        # sub-classes)
        obj._set_format_specifics()
        # Check for unsupported datatypes
        orig_code = header.get_data_dtype()
        try:
            obj.set_data_dtype(orig_code)
        except HeaderDataError:
            raise HeaderDataError('Input header %s has datatype %s but '
                                  'output header %s does not support it'
                                  % (header.__class__,
                                     header.get_value_label('datatype'),
                                     klass))
        if check:
            obj.check_fix()
        return obj

    def _set_format_specifics(self):
        ''' Utility routine to set format specific header stuff
        '''
        pass

    def raw_data_from_fileobj(self, fileobj):
        ''' Read unscaled data array from `fileobj`

        Parameters
        ----------
        fileobj : file-like
           Must be open, and implement ``read`` and ``seek`` methods

        Returns
        -------
        arr : ndarray
           unscaled data array
        '''
        dtype = self.get_data_dtype()
        shape = self.get_data_shape()
        offset = self.get_data_offset()
        return array_from_file(shape, dtype, fileobj, offset, order='C')

    def data_from_fileobj(self, fileobj):
        ''' Read scaled data array from `fileobj`

        Use this routine to get the scaled image data from an image file
        `fileobj`, given a header `self`.  "Scaled" means, with any header
        scaling factors applied to the raw data in the file.  Use
        `raw_data_from_fileobj` to get the raw data.

        Parameters
        ----------
        fileobj : file-like
           Must be open, and implement ``read`` and ``seek`` methods

        Returns
        -------
        arr : ndarray
           scaled data array

        Notes
        -----
        We use the header to get any scale or intercept values to apply to the
        data.  BV files don't have scale factors or intercepts, but
        this routine also works with formats based on Analyze, that do have
        scaling, such as SPM analyze formats and NIfTI.
        '''
        # read unscaled data
        data = self.raw_data_from_fileobj(fileobj)
        # get scalings from header.  Value of None means not present in header
        slope, inter = self.get_slope_inter()
        slope = 1.0 if slope is None else slope
        inter = 0.0 if inter is None else inter
        # Upcast as necessary for big slopes, intercepts
        return apply_read_scaling(data, slope, inter)

    def data_to_fileobj(self, data, fileobj):
        ''' Write `data` to `fileobj`, maybe modifying `self`

        In writing the data, we match the header to the written data, by
        setting the header scaling factors.  Thus we modify `self` in
        the process of writing the data.

        Parameters
        ----------
        data : array-like
           data to write; should match header defined shape
        fileobj : file-like object
           Object with file interface, implementing ``write`` and
           ``seek``

        Examples
        --------
        >>> from nibabel.analyze import AnalyzeHeader
        >>> hdr = AnalyzeHeader()
        >>> hdr.set_data_shape((1, 2, 3))
        >>> hdr.set_data_dtype(np.float64)
        >>> from io import BytesIO
        >>> str_io = BytesIO()
        >>> data = np.arange(6).reshape(1,2,3)
        >>> hdr.data_to_fileobj(data, str_io)
        >>> data.astype(np.float64).tostring('F') == str_io.getvalue()
        True
        '''
        data = np.asanyarray(data)
        shape = self.get_data_shape()
        if data.shape != shape:
            raise HeaderDataError('Data should be shape (%s)' %
                                  ', '.join(str(s) for s in shape))
        out_dtype = self.get_data_dtype()
        try:
            arr_writer = make_array_writer(data,
                                           out_dtype,
                                           self.has_data_slope,
                                           self.has_data_intercept)
        except WriterError as e:
            raise HeaderTypeError(str(e))
        seek_tell(fileobj, self.get_data_offset())
        arr_writer.to_fileobj(fileobj)
        self.set_slope_inter(*get_slope_inter(arr_writer))

    def get_data_dtype(self):
        ''' Get numpy dtype for data

        For examples see ``set_data_dtype``
        '''
        code = int(self._structarr['datatype'])
        dtype = self._data_type_codes.dtype[code]
        return dtype.newbyteorder(self.endianness)

    def set_data_dtype(self, datatype):
        ''' Set numpy dtype for data from code or dtype or type
        '''
        try:
            code = self._data_type_codes[datatype]
        except KeyError:
            raise HeaderDataError(
                'data dtype "%s" not recognized' % datatype)
        dtype = self._data_type_codes.dtype[code]
        # test for void, being careful of user-defined types
        if dtype.type is np.void and not dtype.fields:
            raise HeaderDataError(
                'data dtype "%s" known but not supported' % datatype)
        self._structarr['datatype'] = code

    def get_xflip(self):
        ''' Get xflip for data
        '''
        xflip = int(self._structarr['LRConvention'])
        if xflip == 1:
            return True
        elif xflip == 2:
            return False
        else:
            raise BvError('Left-right convention is unknown!')

    def set_xflip(self, xflip):
        ''' Set xflip for data
        '''
        if xflip == True:
            self._structarr['LRConvention'] = 1
        elif xflip == False:
            self._structarr['LRConvention'] = 2
        else:
            self._structarr['LRConvention'] = 0

    def get_data_shape(self):
        ''' Get shape of data
        '''
        raise NotImplementedError

    def set_data_shape(self, shape):
        ''' Set shape of data
        '''
        raise NotImplementedError

    def get_base_affine(self):
        raise NotImplementedError

    get_best_affine = get_base_affine

    get_default_affine = get_base_affine

    def get_zooms(self):
        raise NotImplementedError

    def set_zooms(self, zooms):
        raise NotImplementedError

    def as_analyze_map(self):
        raise NotImplementedError

    def set_data_offset(self, offset):
        """ Set offset into data file to read data
        """
        self._data_offset = offset

    def get_data_offset(self):
        ''' Return offset into data file to read data
        '''
        return self._data_offset

    def get_slope_inter(self):
        ''' Get scalefactor and intercept

        These are not implemented for BV files
        '''
        return None, None

    def set_slope_inter(self, slope, inter=None):
        ''' Set slope and / or intercept into header

        Set slope and intercept for image data, such that, if the image
        data is ``arr``, then the scaled image data will be ``(arr *
        slope) + inter``

        In this case, for Analyze images, we can't store the slope or the
        intercept, so this method only checks that `slope` is None or 1.0, and
        that `inter` is None or 0.

        Parameters
        ----------
        slope : None or float
            If float, value must be 1.0 or we raise a ``HeaderTypeError``
        inter : None or float, optional
            If float, value must be 0.0 or we raise a ``HeaderTypeError``
        '''
        if (slope is None or slope == 1.0) and (inter is None or inter == 0):
            return
        raise HeaderTypeError('Cannot set slope != 1 or intercept != 0 '
                              'for BV headers')

class BvFileImage(SpatialImage):
    # Set the class of the corresponding header
    header_class = BvFileHeader

    # Set the label ('image') and the extension ('.bv') for a (dummy) BV file
    files_types = (('image', '.bv'),)

    # BV files are not compressed...
    _compressed_exts = ()

    # use the standard ArrayProxy
    ImageArrayProxy = CArrayProxy

    def get_header(self):
        ''' Return header
        '''
        return self._header

    def get_data_dtype(self):
        return self._header.get_data_dtype()

    def set_data_dtype(self, dtype):
        self._header.set_data_dtype(dtype)
    
    @classmethod
    def from_file_map(klass, file_map):
        '''Load image from `file_map`

        Parameters
        ----------
        file_map : None or mapping, optional
           files mapping.  If None (default) use object's ``file_map``
           attribute instead
        '''
        bvf = file_map['image'].get_prepare_fileobj('rb')
        header = klass.header_class.from_fileobj(bvf)
        hdr_copy = header.copy()
        # use row-major memory presentation!
        data = klass.ImageArrayProxy(bvf, hdr_copy)
        img = klass(data, None, header, file_map)
        img._load_cache = {'header': hdr_copy,
                           'affine': None,
                           'file_map': copy_file_map(file_map)}
        return img

    def _write_header(self, header_file, header, slope, inter):
        ''' Utility routine to write header

        Parameters
        ----------
        header_file : file-like
           file-like object implementing ``write``, open for writing
        header : header object
        slope : None or float
           slope for data scaling
        inter : None or float
           intercept for data scaling
        '''
        header.set_slope_inter(slope, inter)
        header.write_to(header_file)

    def _write_data(self, bvfile, data, header):
        ''' Utility routine to write VTC image

        Parameters
        ----------
        vtcfile : file-like
           file-like object implementing ``seek`` or ``tell``, and
           ``write``
        data : array-like
           array to write
        header : analyze-type header object
           header
        '''
        shape = header.get_data_shape()
        if data.shape != shape:
            raise HeaderDataError('Data should be shape (%s)' %
                                  ', '.join(str(s) for s in shape))
        offset = header.get_data_offset()
        out_dtype = header.get_data_dtype()
        array_to_file(data, bvfile, out_dtype, offset)

    def to_file_map(self, file_map=None):
        ''' Write image to `file_map` or contained ``self.file_map``

        Parameters
        ----------
        file_map : None or mapping, optional
           files mapping.  If None (default) use object's ``file_map``
           attribute instead
        '''
        if file_map is None:
            file_map = self.file_map
        data = self.get_data()
        self.update_header()
        hdr = self.get_header()
        out_dtype = self.get_data_dtype()
        arr_writer = make_array_writer(data,
                                       out_dtype,
                                       hdr.has_data_slope,
                                       hdr.has_data_intercept)
        bvf = file_map['image'].get_prepare_fileobj('wb')
        slope, inter = get_slope_inter(arr_writer)
        self._write_header(bvf, hdr, slope, inter)
        # Write image
        shape = hdr.get_data_shape()
        if data.shape != shape:
            raise HeaderDataError('Data should be shape (%s)' %
                                  ', '.join(str(s) for s in shape))
        seek_tell(bvf, hdr.get_data_offset())
        arr_writer.to_fileobj(bvf, order='C')
        bvf.close_if_mine()
        self._header = hdr
        self.file_map = file_map