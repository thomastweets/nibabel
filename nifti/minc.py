import numpy as np

from scipy.io.netcdf import netcdf_file as netcdf

from nifti.spatialimages import SpatialImage
from nifti.volumeutils import allopen

_dt_dict = {
    ('b','unsigned'): np.uint8,
    ('b','signed__'): np.int8,
    ('c','unsigned'): 'S1',
    ('h','unsigned'): np.uint16,
    ('h','signed__'): np.int16,
    ('i','unsigned'): np.uint32,
    ('i','signed__'): np.int32,
    }


class netcdf_fileobj(netcdf):
    def __init__(self, fileobj):
        self._buffer = fileobj
        self._parse()

class MincError(Exception):
    pass

class MINCHeader(object):
    def __init__(self, mincfile, endianness=None, check=True):
        self.endianness = '>'
        self._mincfile = mincfile
        self._image = mincfile.variables['image']
        self._dims = [self._mincfile.variables[s]
                      for s in self._image.dimensions]
        if check:
            self.check_fix()

    @classmethod
    def from_fileobj(klass, fileobj, endianness=None, check=True):
        ncdf_obj = netcdf_fileobj(fileobj)
        return klass(ncdf_obj, endianness, check)

    def check_fix(self):
        for dim in self._dims:
            if dim.spacing != 'regular__':
                raise ValueError('Irregular spacing not supported')
        image_max = self._mincfile.variables['image-max']
        image_min = self._mincfile.variables['image-min']

    def get_data_shape(self):
        return self._image.shape
        
    def get_data_dtype(self):
        typecode = self._image.typecode()
        if typecode == 'f':
            dtt = np.dtype(np.float32)
        elif typecode == 'd':
            dtt = np.dtype(np.float64)
        else:
            signtype = self._image.signtype
            dtt = _dt_dict[(typecode, signtype)]
        return np.dtype(dtt).newbyteorder('>')

    def get_zooms(self):
        return tuple(
            [float(dim.step) for dim in self._dims])

    def get_best_affine(self):
        zooms = self.get_zooms()
        rot_mat = np.eye(3)
        starts = np.zeros((3,))
        for i, dim in enumerate(self._dims):
            rot_mat[:,i] = dim.direction_cosines
            starts[i] = dim.start
        origin = np.dot(rot_mat, starts)
        rz = rot_mat * zooms
        aff = np.eye(4)
        aff[:3,:3] = rot_mat * zooms
        aff[:3,3] = origin
        return aff

    def get_unscaled_data(self):
        dtype = self.get_data_dtype()
        return np.asarray(self._image).view(dtype)

    def _get_valid_range(self):
        ''' Return valid range for image data

        The valid range can come from the image 'valid_range' or
        image 'valid_min' and 'valid_max', or, failing that, from the
        data type range
        '''
        ddt = self.get_data_dtype()
        info = np.iinfo(ddt.type)
        try:
            valid_range = self._image.valid_range
        except AttributeError:
            try:
                valid_range = [self._image.valid_min,
                               self._image.valid_max]
            except AttributeError:
                valid_range = [info.min, info.max]
        if valid_range[0] < info.min or valid_range[1] > info.max:
            raise ValueError('Valid range outside input '
                             'data type range')
        return np.asarray(valid_range, dtype=np.float)

    def _normalize(self, data):
        """
        MINC normalization uses "image-min" and "image-max" variables to
        map the data from the valid range of the NC_TYPE to the range
        specified by "image-min" and "image-max".

        The "image-max" and "image-min" are variables that describe the
        "max" and "min" of image over some dimensions of "image".

        The usual case is that "image" has dimensions ["zspace",
        "yspace", "xspace"] and "image-max" has dimensions
        ["zspace"]. 
        """
        ddt = self.get_data_dtype()
        if ddt.type in np.sctypes['float']:
            return data
        image_max = self._mincfile.variables['image-max']
        image_min = self._mincfile.variables['image-min']
        if image_max.dimensions != image_min.dimensions:
            raise ValueError('"image-max" and "image-min" do not '
                             'have the same dimensions')
        nscales = len(image_max.dimensions)
        img_dims = self._image.dimensions
        if image_max.dimensions != img_dims[:nscales]:
            raise MincError('image-max and image dimensions '
                            'do not match')
        valid_range = self._get_valid_range()
        out_data = np.empty(data.shape, np.float)

        def _norm_slice(sdef):
            imax = image_max[sdef]
            imin = image_min[sdef]
            in_data = np.clip(data[sdef], *valid_range)
            dmin = valid_range[0]
            dmax = valid_range[1]
            sc = (imax-imin) / (dmax-dmin)
            return in_data * sc + (imin - dmin * sc)

        if nscales == 1:
            for i in range(data.shape[0]):
                out_data[i] = _norm_slice(i)
        elif nscales == 2:
            for i in range(data.shape[0]):
                for j in range(data.shape[1]):
                    out_data[i,j] = _norm_slice((i,j))
        else:
            raise MincError('More than two scaling dimensions')
        return out_data

    def get_scaled_data(self):
        return self._normalize(self.get_unscaled_data())
    

class MINCImage(SpatialImage):
    _header_maker = MINCHeader
    
    def _set_header(self, header):
        self._header = header

    def get_data(self):
        ''' Lazy load of data '''
        if not self._data is None:
            return self._data
        cdf = self._header
        self._data = self._header.get_scaled_data()
        return self._data

    def get_shape(self):
        if not self._data is None:
            return self._data.shape
        return self._header.get_data_shape()
    
    def get_data_dtype(self):
        return self._header.get_data_dtype()
    
    @classmethod
    def from_filespec(klass, filespec):
        files = klass.filespec_to_files(filespec)
        return klass.from_files(files)
    
    @classmethod
    def from_files(klass, files):
        fname = files['image']
        header = klass._header_maker.from_fileobj(allopen(fname))
        affine = header.get_best_affine()
        ret =  klass(None, affine, header)
        ret._files = files
        return ret
    
    @classmethod
    def from_image(klass, img):
        return klass(img.get_data(),
                     img.get_affine(),
                     img.get_header(),
                     img.extra)
    
    @staticmethod
    def filespec_to_files(filespec):
        return {'image':filespec}
        
    @classmethod
    def load(klass, filespec):
        return klass.from_filespec(filespec)


load = MINCImage.load
