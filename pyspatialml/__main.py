import numpy as np
import rasterio
import tempfile
from tqdm import tqdm
from rasterio.transform import Affine
from rasterio.windows import Window


def _predfun(img, estimator):
    """Prediction function for classification or regression response

    Parameters
    ----------
    img : 3d numpy array of raster data

    estimator : estimator object implementing 'fit'
        The object to use to fit the data

    Returns
    -------
    result_cla : 2d numpy array
        Single band raster as a 2d numpy array containing the
        classification or regression result"""

    n_features, rows, cols = img.shape[0], img.shape[1], img.shape[2]

    # reshape each image block matrix into a 2D matrix
    # first reorder into rows,cols,bands(transpose)
    # then resample into 2D array (rows=sample_n, cols=band_values)
    n_samples = rows * cols
    flat_pixels = img.transpose(1, 2, 0).reshape(
        (n_samples, n_features))

    # create mask for NaN values and replace with number
    flat_pixels_mask = flat_pixels.mask.copy()

    # prediction
    result_cla = estimator.predict(flat_pixels)

    # replace mask
    result_cla = np.ma.masked_array(
        data=result_cla, mask=flat_pixels_mask.any(axis=1))

    # reshape the prediction from a 1D into 3D array [band, row, col]
    result_cla = result_cla.reshape((1, rows, cols))

    return result_cla


def _probfun(img, estimator):
    """Class probabilities function

    Parameters
    ----------
    img : 3d numpy array of raster data

    estimator : estimator object implementing 'fit'
        The object to use to fit the data

    Returns
    -------
    result_proba : 3d numpy array
        Multi band raster as a 3d numpy array containing the
        probabilities associated with each class.
        Array is in (class, row, col) order"""

    n_features, rows, cols = img.shape[0], img.shape[1], img.shape[2]

    # reshape each image block matrix into a 2D matrix
    # first reorder into rows,cols,bands(transpose)
    # then resample into 2D array (rows=sample_n, cols=band_values)
    n_samples = rows * cols
    flat_pixels = img.transpose(1, 2, 0).reshape(
        (n_samples, n_features))

    # create mask for NaN values and replace with number
    flat_pixels_mask = flat_pixels.mask.copy()

    # predict probabilities
    result_proba = estimator.predict_proba(flat_pixels)

    # reshape class probabilities back to 3D image [iclass, rows, cols]
    result_proba = result_proba.reshape(
        (rows, cols, result_proba.shape[1]))
    flat_pixels_mask = flat_pixels_mask.reshape((rows, cols, n_features))

    # flatten mask into 2d
    mask2d = flat_pixels_mask.any(axis=2)
    mask2d = np.where(mask2d != mask2d.min(), True, False)
    mask2d = np.repeat(mask2d[:, :, np.newaxis],
                       result_proba.shape[2], axis=2)

    # convert proba to masked array using mask2d
    result_proba = np.ma.masked_array(
        result_proba,
        mask=mask2d,
        fill_value=np.nan)

    # reshape band into rasterio format [band, row, col]
    result_proba = result_proba.transpose(2, 0, 1)

    return result_proba


def _maximum_dtype(src):
    """Returns a single dtype that is large enough to store data
    within all raster bands

    Parameters
    ----------
    src : rasterio.io.DatasetReader
        Rasterio datasetreader in the opened mode

    Returns
    -------
    dtype : str
        Dtype that is sufficiently large to store all raster
        bands in a single numpy array"""

    if 'complex128' in src.dtypes:
        dtype = 'complex128'
    elif 'complex64' in src.dtypes:
        dtype = 'complex64'
    elif 'complex' in src.dtypes:
        dtype = 'complex'
    elif 'float64' in src.dtypes:
        dtype = 'float64'
    elif 'float32' in src.dtypes:
        dtype = 'float32'
    elif 'int32' in src.dtypes:
        dtype = 'int32'
    elif 'uint32' in src.dtypes:
        dtype = 'uint32'
    elif 'int16' in src.dtypes:
        dtype = 'int16'
    elif 'uint16' in src.dtypes:
        dtype = 'uint16'
    elif 'uint16' in src.dtypes:
        dtype = 'uint16'
    elif 'bool' in src.dtypes:
        dtype = 'bool'

    return dtype


def predict(estimator, dataset, file_path=None, predict_type='raw',
            indexes=None, driver='GTiff', dtype='float32', nodata=-99999):
    """Apply prediction of a scikit learn model to a GDAL-supported
    raster dataset

    Parameters
    ----------
    estimator : estimator object implementing 'fit'
        The object to use to fit the data

    dataset : rasterio.io.DatasetReader
        An opened Rasterio DatasetReader

    file_path : str, optional
        Path to a GeoTiff raster for the classification results
        If not supplied then output is written to a temporary file

    predict_type : str, optional (default='raw')
        'raw' for classification/regression
        'prob' for probabilities

    indexes : List, int, optional
        List of class indices to export

    driver : str, optional. Default is 'GTiff'
        Named of GDAL-supported driver for file export

    dtype : str, optional. Default is 'float32'
        Numpy data type for file export

    nodata : any number, optional. Default is -99999
        Nodata value for file export

    Returns
    -------
    rasterio.io.DatasetReader with predicted raster"""

    src = dataset

    # chose prediction function
    if predict_type == 'raw':
        predfun = _predfun
    elif predict_type == 'prob':
        predfun = _probfun

    # determine output count
    if predict_type == 'prob' and isinstance(indexes, int):
        indexes = range(indexes, indexes+1)

    elif predict_type == 'prob' and indexes is None:
        img = src.read(masked=True, window=(0, 0, 1, src.width))
        n_features, rows, cols = img.shape[0], img.shape[1], img.shape[2]
        n_samples = rows * cols
        flat_pixels = img.transpose(1, 2, 0).reshape(
            (n_samples, n_features))
        result = estimator.predict_proba(flat_pixels)
        indexes = range(result.shape[0])

    elif predict_type == 'raw':
        indexes = range(1)

    # open output file with updated metadata
    meta = src.meta
    meta.update(driver=driver, count=len(indexes), dtype=dtype, nodata=nodata)

    # optionally output to a temporary file
    if file_path is None:
        file_path = tempfile.NamedTemporaryFile().name

    with rasterio.open(file_path, 'w', **meta) as dst:

        # define windows
        windows = [window for ij, window in dst.block_windows()]

        # generator gets raster arrays for each window
        # read all bands if single dtype
        if src.dtypes.count(src.dtypes[0]) == len(src.dtypes):
            data_gen = (src.read(window=window, masked=True)
                        for window in windows)

        # else read each band separately
        else:
            def read(src, window):
                dtype = _maximum_dtype(src)
                arr = np.ma.zeros((src.count, window.height, window.width),
                                  dtype=dtype)

                for band in range(src.count):
                    arr[band, :, :] = src.read(
                        band+1, window=window, masked=True)

                return arr

            data_gen = (read(src=src, window=window) for window in windows)

        with tqdm(total=len(windows)) as pbar:
            for window, arr in zip(windows, data_gen):
                result = predfun(arr, estimator)
                result = np.ma.filled(result, fill_value=nodata)
                dst.write(result[indexes, :, :].astype(dtype), window=window)
                pbar.update(1)

    return rasterio.open(file_path)


def calc(dataset, function, file_path=None, driver='GTiff', dtype='float32',
         nodata=-99999):
    """Apply prediction of a scikit learn model to a GDAL-supported
    raster dataset

    Parameters
    ----------
    dataset : rasterio.io.DatasetReader
        An opened Rasterio DatasetReader

    function : function that takes an numpy array as a single argument

    file_path : str, optional
        Path to a GeoTiff raster for the classification results
        If not supplied then output is written to a temporary file

    driver : str, optional. Default is 'GTiff'
        Named of GDAL-supported driver for file export

    dtype : str, optional. Default is 'float32'
        Numpy data type for file export

    nodata : any number, optional. Default is -99999
        Nodata value for file export

    Returns
    -------
    rasterio.io.DatasetReader containing result of function output"""

    src = dataset

    # determine output dimensions
    img = src.read(masked=True, window=(0, 0, 1, src.width))
    arr = function(img)
    if len(arr.shape) > 2:
        indexes = range(arr.shape[0])
    else:
        indexes = 1

    # optionally output to a temporary file
    if file_path is None:
        file_path = tempfile.NamedTemporaryFile().name

    # open output file with updated metadata
    meta = src.meta
    meta.update(driver=driver, count=len(indexes), dtype=dtype, nodata=nodata)

    with rasterio.open(file_path, 'w', **meta) as dst:

        # define windows
        windows = [window for ij, window in dst.block_windows()]

        # generator gets raster arrays for each window
        # read all bands if single dtype
        if src.dtypes.count(src.dtypes[0]) == len(src.dtypes):
            data_gen = (src.read(window=window, masked=True)
                        for window in windows)

        # else read each band separately
        else:
            def read(src, window):
                dtype = _maximum_dtype(src)
                arr = np.ma.zeros((src.count, window.height, window.width),
                                  dtype=dtype)

                for band in range(src.count):
                    arr[band, :, :] = src.read(
                        band+1, window=window, masked=True)

                return arr

            data_gen = (read(src=src, window=window) for window in windows)

        with tqdm(total=len(windows)) as pbar:

            for window, arr in zip(windows, data_gen):
                result = function(arr)
                result = np.ma.filled(result, fill_value=nodata)
                dst.write(result.astype(dtype), window=window)
                pbar.update(1)

    return rasterio.open(file_path)


def crop(dataset, bounds, file_path=None, driver='GTiff'):
    """Crops a rasterio dataset by the supplied bounds

    dataset : rasterio.io.DatasetReader
        An opened Rasterio DatasetReader
    
    bounds : tuple
        A tuple containing the bounding box to clip by in the
        form of (xmin, xmax, ymin, ymax)
    
    file_path : str, optional. Default=None
        File path to save to cropped raster.
        If not supplied then the cropped raster is saved to a
        temporary file
    
    driver : str, optional. Default is 'GTiff'
        Named of GDAL-supported driver for file export
    
    Returns
    -------
    rasterio.io.DatasetReader with the cropped raster"""

    src = dataset
    
    xmin, xmax, ymin, ymax = bounds

    rows, cols = rasterio.transform.rowcol(
    rasterio.open(strata).transform, xs=(xmin, xmax), ys=(ymin, ymax))

    cropped_arr = src.read(1, window=Window(col_off=min(cols),
                                            row_off=min(rows),
                                            width=max(cols)-min(cols),
                                            height=max(rows)-min(rows)))

    meta = src.meta
    aff = src.transform
    meta['width'] = max(cols)-min(cols)
    meta['height'] = max(rows)-min(rows)
    meta['transform'] = Affine(aff.a, aff.b, xmin, aff.d, aff.e, ymin)
    meta['driver'] = driver

    if file_path is None:
        file_path = tempfile.NamedTemporaryFile().name

    with rasterio.open(file_path, 'w', **meta) as dst:
        dst.write(cropped_arr)
    
    return rasterio.open(file_path)
