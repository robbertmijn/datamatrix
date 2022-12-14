# -*- coding: utf-8 -*-

"""
This file is part of datamatrix.

datamatrix is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

datamatrix is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with datamatrix.  If not, see <http://www.gnu.org/licenses/>.
"""
import logging
import os
import weakref
from datamatrix.py3compat import *
from datamatrix._datamatrix._numericcolumn import NumericColumn, FloatColumn
from datamatrix._datamatrix._datamatrix import DataMatrix
try:
    from collections.abc import Sequence  # Python 3.3 and later
except ImportError:
    from collections import Sequence
from collections import OrderedDict
try:
    import numpy as np
    from numpy import nanmean, nanmedian, nanstd
except ImportError:
    np = None
try:
    Ellipsis
except NameError:
    Ellipsis = None  # was introduced in Python 3.10
try:
    import psutil
except ImportError:
    psutil = None
logger = logging.getLogger('datamatrix')

# Memory consumption should be kept below either the relative or absolute
# minimum. Beyond these limits, memmap will be used.
MIN_MEM_FREE_REL = .5
MIN_MEM_FREE_ABS = 4294967296
MB = 1048576


class _MultiDimensionalColumn(NumericColumn):

    """
    desc:
        A column in which each cell is a float array.
    """

    dtype = float
    printoptions = dict(precision=4, threshold=4, edgeitems=2)
    touch_history = OrderedDict()

    def __init__(self, datamatrix, shape, defaultnan=True, **kwargs):

        """
        desc:
            Constructor. You generally don't call this constructor correctly,
            but use the MultiDimensional helper function.

        arguments:
            datamatrix:
                desc: The DataMatrix to which this column belongs.
                type: DataMatrix
            shape:
                desc: A tuple that specifies the number and size of the
                      dimensions of each cell. Values can be integers, or 
                      tuples of non-integer values that specify names of
                      indices, e.g. `shape=(('x', 'y'), 10)). Importantly,
                      the length of the column (the number of rows) is
                      implicitly used as the first dimension. That, if you
                      specify a two-dimensional shape, then the resulting
                      column has three dimensions.
                type: int
            defaultnan:
                desc: Indicates whether the column should be initialized with
                      `nan` values (`True`) or 0s (`False`).
                type: bool
        """

        if np is None:
            raise Exception(
                'NumPy and SciPy are required, but not installed.')
        normshape = tuple()
        if isinstance(shape, (int, str)):
            shape = (shape, )
        self._dim_names = []
        for dim_size in shape:
            # Dimension without named indices
            if isinstance(dim_size, int):
                normshape += (dim_size, )
                self._dim_names.append(list(range(dim_size)))
            else:
                normshape += (len(dim_size), )
                self._dim_names.append(dim_size)
        self._shape = normshape
        self.defaultnan = defaultnan
        self._fd = None
        self._loaded = kwargs.get('loaded', None)
        NumericColumn.__init__(self, datamatrix, **kwargs)
        
    def __del__(self):
        """Clean up weak references on destruction."""
        id_ = id(self)
        if id_ in self.touch_history:
            self.touch_history.pop(id_)

    def setallrows(self, value):

        """
        desc:
            Sets all rows to a value, or array of values.

        arguments:
            value:	A value, or array of values that has the same length as the
                    shape of the column.
        """

        value = self._checktype(value)
        self._seq[:] = value

    @property
    def unique(self):

        raise NotImplementedError(
            'unique is not implemented for {}'.format(self.__class__.__name__))

    @property
    def shape(self):

        """
        name: shape

        desc:
            A property to access and change the shape of the column. The shape
            includes the length of the column (the number of rows) as the first
            dimension.
        """

        return (len(self), ) + self._shape
    
    @property
    def depth(self):
        
        """
        name: depth

        desc:
            A property to access and change the depth of the column. The depth
            is the second dimension of the column, where the length of the
            column (the number of rows) is the first dimension.
        """
        
        return self._shape[0]

    @depth.setter
    def depth(self, depth):
        if depth == self.depth:
            return
        if len(self._shape) > 2:
            raise TypeError('Can only change the depth of two-dimensional '
                            'MultiDimensionalColumns/ SeriesColumns')
        if depth > self.depth:
            seq = np.zeros((len(self), depth), dtype=self.dtype)
            if self.defaultnan:
                seq[:] = np.nan
            seq[:, :self.depth] = self._seq
            self._seq = seq
            self._shape = (depth, )
            return
        self._shape = (depth, )
        self._seq = self._seq[:, :depth]

    @property
    def plottable(self):

        """
        name: plottable

        desc:
            Gives a view of the traces where the axes have been swapped. This 
            is the format that matplotlib.pyplot.plot() expects.
        """

        return np.swapaxes(self._seq, 0, -1)

    @property
    def mean(self):

        return nanmean(self._seq, axis=0)

    @property
    def median(self):

        return nanmedian(self._seq, axis=0)

    @property
    def std(self):

        return nanstd(self._seq, axis=0, ddof=1)

    @property
    def max(self):

        return np.nanmax(self._seq, axis=0)

    @property
    def min(self):

        return np.nanmin(self._seq, axis=0)

    @property
    def sum(self):

        return np.nansum(self._seq, axis=0)
        
    @property
    def loaded(self):
        """
        name: loaded

        desc:
            A property to unloaded the column to disk (by assigning `False`)
            and load the column from disk (by assigning `True`). You don't
            usually change this property manually, but rather let the built-in
            memory management decide when and columns need to be (un)loaded.
        """
        if self._loaded is None:
            self._loaded = self._sufficient_free_memory()
        return self._loaded
    
    @loaded.setter
    def loaded(self, val):
        if val == self._loaded:
            return
        logger.debug('{} column {}'.format('loading' if val else 'unloading',
                                           id(self)))
        self._loaded = val
        # Simply assigning self._seq will trigger a conversion to/ from
        # np.memmap based on the self._loaded
        self._seq = self._seq

    # Private functions
    
    def _memory_size(self):
        """Returns the size of the column in bytes if it were loaded into
        memory.
        """
        size = np.empty(1, dtype=self.dtype).nbytes
        for dim_size in self.shape:
            if isinstance(dim_size, Sequence):
                dim_size = len(dim_size)
            size *= dim_size
        return size
    
    def _sufficient_free_memory(self):
        """Returns whether there is sufficient free memory for the current
        column to be loaded into memory based on the MIN_MEM_FREE_ABS and
        MIN_MEM_FREE_REL constants.
        """
        vm = psutil.virtual_memory()
        mem_free_abs = vm.available - self._memory_size()
        mem_free_rel = mem_free_abs / vm.total
        logger.debug('{} MB available ({:.2f}%)'.format(mem_free_abs // MB,
                                                        100 * mem_free_rel))
        return mem_free_abs > MIN_MEM_FREE_ABS or \
            mem_free_rel > MIN_MEM_FREE_REL
    
    def _touch(self):
        """Moves the current column to the end of the touch history to indicate
        that it was used last.
        """
        id_ = id(self)
        if id_ in self.touch_history:
            self.touch_history.move_to_end(id_)
        else:
            self.touch_history[id_] = weakref.ref(self)
        for col in self.touch_history.values():
            col = col()
            if col is None or col is self or not col.loaded:
                continue
            if not self._sufficient_free_memory():
                logger.debug('insufficient free memory')
                col.loaded = False
        if not self.loaded and self._sufficient_free_memory():
            logger.debug('loading currently touched column')
            self.loaded = True

    def _init_seq(self):
        if self.loaded:
            # Close previous memmap object
            if self._fd is not None and not self._fd.closed:
                self._fd.close()
                self._fd = None
            logger.debug('initializing loaded array')
            if self.defaultnan:
                self._seq = np.empty(self.shape, dtype=self.dtype)
                self._seq[:] = np.nan
            else:
                self._seq = np.zeros(self.shape, dtype=self.dtype)
            return
        # Use memory-mapped array
        import tempfile
        logger.debug('initializing unloaded array (memmap)')
        self._fd = tempfile.NamedTemporaryFile(dir=os.getcwd())
        self._seq = np.memmap(self._fd, shape=self.shape, dtype=self.dtype)
        self._seq[:] = np.nan if self.defaultnan else 0
            
    def __setattr__(self, key, val):
        """Catches assignments to the _seq attribute, which may need to be
        converted to memmap or a regular array depending on the loaded status.
        """
        if key != '_seq' or (isinstance(val, np.memmap) and not self.loaded) \
                or (not isinstance(val, np.memmap) and self.loaded):
            super().__setattr__(key, val)
            return
        # Convert to memory-mapped array
        self._init_seq()
        self._seq[:] = val

    def _printable_list(self):
        with np.printoptions(**self.printoptions):
            return [str(cell) for cell in self]

    def _operate(self, a, number_op, str_op=None, flip=False):

        self._touch()
        # For a 1D array with the length of the datamatrix, we create an array
        # in which the second dimension (i.e. the shape) is constant. This
        # allows us to do by-row operations.
        if isinstance(a, (list, tuple)):
            a = np.array(a, dtype=self.dtype)
        if isinstance(a, NumericColumn):
            a = np.array(a._seq)
        if isinstance(a, np.ndarray) and a.shape == (len(self), ):
            a2 = np.empty(self.shape, dtype=self.dtype)
            np.swapaxes(a2, 0, -1)[:] = a
            a = a2
        rowid = self._rowid.copy()
        seq = number_op(a, self._seq) if flip else number_op(self._seq, a)
        return self._empty_col(rowid=rowid, seq=seq)

    def _map(self, fnc):

        # For a MultiDimensionalColumn, we need to make a special case, because
        # the shape of the new MultiDimensionalColumn may be different from
        # the shape of the original column.
        for i, cell in enumerate(self):
            a = fnc(cell)
            if not i:
                newcol = self.__class__(self.dm, shape=len(a))
            newcol[i] = a
        return newcol

    def _checktype(self, value):

        try:
            a = np.empty(self.shape[1:], dtype=self.dtype)
            a[:] = value
        except:
            raise Exception('Invalid type: %s' % str(value))
        return a

    def _compare(self, other, op):

        raise NotImplementedError('Cannot compare {}s'.format(
            self.__class__.__name__))

    def _tosequence(self, value, length):
        
        full_shape = (length, ) + self.shape[1:]
        # For float and integers, we simply create a new (length, shape) array
        # with only this value
        if isinstance(value, (float, int)):
            a = np.empty(full_shape, dtype=self.dtype)
            a[:] = value
            return a
        try:
            a = np.array(value, dtype=self.dtype)
        except:
            raise TypeError('Cannot convert to sequence: %s' % str(value))
        # For a 1D array with the length of the datamatrix, we create an array
        # in which the second dimension (i.e. the shape) is constant.
        if a.shape == (length, ):
            a2 = np.empty(full_shape, dtype=self.dtype)
            np.swapaxes(a2, 0, -1)[:] = a
            return a2
        # For a 2D array that already has the correct dimensions, we return it.
        if a.shape == full_shape:
            return a
        # Set all rows at once
        if a.shape == self.shape[1:]:
            return a
        raise TypeError('Cannot convert to sequence: %s' % str(value))

    def _empty_col(self, datamatrix=None, **kwargs):

        return self.__class__(datamatrix if datamatrix else self._datamatrix,
                              shape=self.shape[1:], defaultnan=self.defaultnan,
                              **kwargs)

    def _addrowid(self, _rowid):

        old_length = len(self)
        self._rowid = np.concatenate((self._rowid, _rowid.asarray))
        a = np.zeros((len(self._rowid), ) + self.shape[1:], dtype=self.dtype)
        a[:old_length] = self._seq
        self._seq = a

    def _getintkey(self, key):

        return self._seq[key]

    # Implemented syntax

    def __getitem__(self, key):
        self._touch()
        if isinstance(key, tuple) and len(key) <= len(self._seq.shape):
            # Advanced indexing always returns a copy, rather than a view, so
            # there's no need to explicitly copy the result.
            indices = self._numindices(key)
            value = self._seq[indices]
            # If the index refers to exactly one value, then we return this
            # value as a float.
            if len(key) == len(self._seq.shape) and all(
                    self._single_index(index) for index in key):
                return float(value.squeeze())
            # If the index refers to exactly one row, then we return the cell
            # as an array
            if self._single_index(key[0]):
                return value[0]
            # Otherwise we create a new MultiDimensionalColumn. The shape is
            # squeezed such that all dimensions of size 1 are removed, except
            # the first dimension (in case there is a single row).
            squeeze_dims = tuple(dim + 1
                                 for dim, size in enumerate(value.shape[1:])
                                 if size == 1)
            # print('squeeze', squeeze_dims)
            value = value.squeeze(axis=squeeze_dims)
            # If only one dimension remains, we return a FloatColumn, otherwise
            # a MultiDimensionalColumn, SeriesColumn, SurfaceColumn, or
            # VolumeColumn. However, we don't do this if any of the dimensions
            # was specified as a slice or ellipsis, because in that case it's
            # a coincidence.
            row_indices = indices[0].flatten()
            if len(value.shape) == 1 and not any(
                    isinstance(index, slice) or index == Ellipsis
                    for index in key[1:]):
                col = FloatColumn(self._datamatrix,
                                  rowid=self._rowid[row_indices],
                                  seq=value)
            else:
                # This captures the edge case in which a slice happened to
                # result in a one-dimensional column. In this case, we expand
                # it back to two dimensions.
                if len(value.shape) == 1:
                    value = value.reshape(value.shape[0], 1)
                if len(value.shape) == 2:
                    from datamatrix._datamatrix._seriescolumn import \
                        _SeriesColumn
                    cls = _SeriesColumn
                else:
                    cls = _MultiDimensionalColumn
                col = cls(self._datamatrix, shape=value.shape[1:],
                          rowid=self._rowid[row_indices],
                          seq=value)
            return col
        return super().__getitem__(key)

    def __setitem__(self, key, value):
        self._touch()
        if not isinstance(key, tuple):
            key = (key, )
            key_was_tuple = False
        else:
            key_was_tuple = True
        indices = self._numindices(key)
        # When assigning, the shape of the indexed array (target) and the
        # to-be-assigned value either need to match, or the shape of the
        # value needs to match the end of the shape of the target.
        if isinstance(value, (Sequence, np.ndarray)):
            if not isinstance(value, np.ndarray):
                value = np.array(value)
            target_shape = self._seq[indices].shape
            # This is an edge case that is preserved for backwards
            # compatibility: for a 2D column, and only if a single key is
            # provided (not a tuple), then values that match the length of the
            # datamatrix are used to set all values in each row to a constant
            # value, like so:
            #
            # dm = DataMatrix(length=2)
            # dm.col = SeriesColumn(depth=2)
            # dm.col = 1, 2
            # print(dm)
            # +---+---------+
            #| # |   col   |
            #+---+---------+
            #| 0 | [1. 1.] |
            #| 1 | [2. 2.] |
            if len(self.shape) == 2 and value.shape == (target_shape[0], ) \
                    and not key_was_tuple:
                np.rot90(self._seq)[:] = value
                return
            elif target_shape[-len(value.shape):] != value.shape:
                value = value.reshape(target_shape)
        self._seq[indices] = value
        self._datamatrix._mutate()

    def _single_index(self, index):
        return not isinstance(index, (slice, Sequence, np.ndarray)) and not \
            index == Ellipsis
        
    def _named_index(self, name, dim):
        try:
            return self._dim_names[dim - 1].index(name)
        except ValueError as e:
            raise ValueError('{} is not an index name'.format(name))
        
    def _numindices(self, indices):
        """Takes a tuple of indices, which can be either named or numeric,
        and converts it to a tuple of numeric indices that can be used to
        slice the array.
        """
        # An ellipsis (...) is expanded to a full slice that covers all
        # dimensions that weren't specified. In other words, say that col
        # has 3 dimensions (+ 1 dimension for the rows), then
        # dm.col[0, ..., 0] becomes dm.col[0, :, :, 0]
        if Ellipsis is not None and Ellipsis in indices:
            if indices.count(Ellipsis) > 1:
                raise IndexError(
                    'at most one ellipsis (...) allowed in indexing')
            indices = indices[:indices.index(Ellipsis)] + \
                (slice(None), ) * (1 + len(self._seq.shape) - len(indices)) + \
                indices[indices.index(Ellipsis) + 1:]
        # Indices can be specified as slices, integers, names, and sequences of
        # integers and/ or names. These are all normalized to numpy arrays of
        # indices. The result is a tuple of arrays, where each array indexes
        # one dimension.
        numindices = tuple()
        for dim, index in enumerate(indices):
            if isinstance(index, slice):
                index = np.arange(*index.indices(self._seq.shape[dim]))
            elif isinstance(index, int):
                index = np.array([index])
            elif isinstance(index, np.ndarray):
                pass
            elif isinstance(index, DataMatrix):
                if index != self._datamatrix:
                    raise ValueError('Cannot slice column with a different DataMatrix')
                index = np.searchsorted(self._rowid, index._rowid)
            elif isinstance(index, Sequence) and not isinstance(index, str):
                index = np.array([
                    name if isinstance(name, int)
                    else self._named_index(name, dim)
                    for name in index])
            else:
                index = np.array([self._named_index(index, dim)])
            numindices += (index, )
        # By default, NumPy interprets tuples of indexes in a zip-like way, so
        # that a[(0, 2), (1, 3)] indexes two cells a[0, 1] and a[2, 3]. We want
        # tuples to be interpreted such that they refer to rows 0 and 2 and
        # columns 1 and 3. This is done with np.ix_().
        return np.ix_(*numindices)


def MultiDimensionalColumn(shape, defaultnan=True, **kwargs):
    
    return _MultiDimensionalColumn, dict(shape=shape, defaultnan=defaultnan,
                                         **kwargs)
