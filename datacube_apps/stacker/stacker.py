"""
Create time-stacked NetCDF files

"""
from __future__ import absolute_import, print_function

import copy
import datetime
import itertools
import logging
import os
from functools import partial

import click
import dask.array as da
import dask
from dateutil import tz
import pandas as pd
from pandas.tseries.offsets import YearBegin, YearEnd
from pathlib import Path

import datacube
from datacube.api import Tile
from datacube.model.utils import xr_apply, datasets_to_doc
from datacube.storage import netcdf_writer
from datacube.storage.storage import create_netcdf_storage_unit
from datacube.ui import task_app
from datacube.ui.click import to_pathlib


_LOG = logging.getLogger(__name__)


APP_NAME = 'datacube-stacker'


def get_filename(config, cell_index, year):
    file_path_template = str(Path(config['location'], config['file_path_template']))
    return file_path_template.format(tile_index=cell_index, start_time=year, version=config['taskfile_version'])


def make_stacker_tasks(index, config, **kwargs):
    product = config['product']
    query = {kw: arg for kw, arg in kwargs.items() if kw in ['time', 'cell_index'] and arg is not None}

    gw = datacube.api.GridWorkflow(index=index, product=product.name)
    cells = gw.list_cells(product=product.name, **query)
    for (cell_index, tile) in cells.items():
        for (year, year_tile) in _split_by_year(tile):
            storage_files = set(ds.local_path for ds in itertools.chain(*year_tile.sources.values))
            if len(storage_files) > 1:
                year_tile = gw.update_tile_lineage(year_tile)
                output_filename = get_filename(config, cell_index, year)
                _LOG.info('Stacking required for: year=%d, cell=%s. Output=%s', year, cell_index, output_filename)
                yield dict(year=year,
                           tile=year_tile,
                           cell_index=cell_index,
                           output_filename=output_filename)
            elif len(storage_files) == 1:
                [only_filename] = storage_files
                _LOG.info('Stacking not required for: year=%d, cell=%s. existing=%s', year, cell_index, only_filename)


def make_stacker_config(index, config, export_path=None, **query):
    config['product'] = index.products.get_by_name(config['output_type'])

    if export_path is not None:
        config['location'] = export_path
        config['index_datasets'] = False
    else:
        config['index_datasets'] = True

    if not os.access(config['location'], os.W_OK):
        _LOG.warning('Current user appears not have write access output location: %s', config['location'])

    chunking = config['storage']['chunking']
    chunking = [chunking[dim] for dim in config['storage']['dimension_order']]

    var_param_keys = {'zlib', 'complevel', 'shuffle', 'fletcher32', 'contiguous', 'attrs'}
    variable_params = {}
    for mapping in config['measurements']:
        varname = mapping['name']
        variable_params[varname] = {k: v for k, v in mapping.items() if k in var_param_keys}
        variable_params[varname]['chunksizes'] = chunking

    config['variable_params'] = variable_params

    config['taskfile_version'] = int(datacube.utils.datetime_to_seconds_since_1970(datetime.datetime.now()))

    return config


def _split_by_year(tile, time_dim='time'):
    start_range = tile.sources[time_dim][0].data
    end_range = tile.sources[time_dim][-1].data

    for date in pd.date_range(start=YearBegin(normalize=True).rollback(start_range),
                              end=end_range,
                              freq='AS',
                              normalize=True):
        sources_slice = tile.sources.loc[{time_dim: slice(date, YearEnd(normalize=True).rollforward(date))}]
        year_str = '{0:%Y}'.format(date)
        yield year_str, Tile(sources=sources_slice, geobox=tile.geobox)


def get_history_attribute(config, task):
    return '{dt} {user} {app} ({ver}) {args}  # {comment}'.format(
        dt=datetime.datetime.now(tz.tzlocal()).isoformat(),
        user=os.environ.get('USERNAME') or os.environ.get('USER'),
        app=APP_NAME,
        ver=datacube.__version__,
        args=', '.join([config['app_config_file'],
                        str(config['version']),
                        str(config['taskfile_version']),
                        task['output_filename'],
                        str(task['year']),
                        str(task['cell_index'])
                       ]),
        comment='Stacking datasets for a year into a single NetCDF file'
    )


def _unwrap_dataset_list(labels, dataset_list):
    return dataset_list[0]


def do_stack_task(config, task):
    global_attributes = config['global_attributes']
    global_attributes['history'] = get_history_attribute(config, task)

    variable_params = config['variable_params']

    output_filename = Path(task['output_filename'])
    output_uri = output_filename.absolute().as_uri()
    tile = task['tile']

    # Only use the time chunk size (eg 5), but not spatial chunks
    # This means the file only gets opened once per band, and all data is available when compressing on write
    # 5 * 4000 * 4000 * 2bytes == 152MB, so mem usage is not an issue
    chunk_profile = {'time': config['storage']['chunking']['time']}

    data = datacube.api.GridWorkflow.load(tile, dask_chunks=chunk_profile)

    unwrapped_datasets = xr_apply(tile.sources, _unwrap_dataset_list, dtype='O')
    data['dataset'] = datasets_to_doc(unwrapped_datasets)

    nco = create_netcdf_storage_unit(output_filename,
                                     data.crs,
                                     data.coords,
                                     data.data_vars,
                                     variable_params,
                                     global_attributes)

    for name, variable in data.data_vars.items():
        try:
            with dask.set_options(get=dask.async.get_sync):
                da.store(variable.data, nco[name], lock=True)
        except ValueError:
            nco[name][:] = netcdf_writer.netcdfy_data(variable.values)
        nco.sync()
    nco.close()

    if config.get('check_data_identical', False):
        new_tile = make_updated_tile(unwrapped_datasets, output_uri, tile.geobox)
        new_data = datacube.api.GridWorkflow.load(new_tile, dask_chunks=chunk_profile)

        with dask.set_options(get=dask.async.get_sync):
            if not all((data == new_data).all().values()):
                _LOG.error("Mismatch found for %s, not indexing", output_filename)
                raise ValueError("Mismatch found for %s, not indexing" % output_filename)

    return unwrapped_datasets, output_uri


def make_updated_tile(old_datasets, new_uri, geobox):
    def update_dataset_location(labels, dataset):
        new_dataset = copy.copy(dataset)
        new_dataset.local_uri = new_uri
        return [dataset]

    updated_datasets = xr_apply(old_datasets, update_dataset_location, dtype='O')
    return datacube.api.Tile(sources=updated_datasets, geobox=geobox)


def process_result(index, result):
    datasets, new_uri = result
    for dataset in datasets.values:
        _LOG.info('Updating dataset location: %s', dataset.local_path)
        old_uri = dataset.local_uri
        index.datasets.add_location(dataset.id, new_uri)
        index.datasets.archive_location(dataset.id, old_uri)


@click.command(name=APP_NAME)
@datacube.ui.click.pass_index(app_name=APP_NAME)
@datacube.ui.click.global_cli_options
@click.option('--cell-index', 'cell_index', help='Limit the process to a particular cell (e.g. 14,-11)',
              callback=task_app.validate_cell_index, default=None)
@click.option('--year', 'time', callback=task_app.validate_year, help='Limit the process to a particular year')
@click.option('--export-path', 'export_path',
              help='Write the stacked files to an external location without updating the index',
              default=None,
              type=click.Path(exists=True, writable=True, file_okay=False))
@task_app.queue_size_option
@task_app.task_app_options
@task_app.task_app(make_config=make_stacker_config, make_tasks=make_stacker_tasks)
def main(index, config, tasks, executor, queue_size, **kwargs):
    click.echo('Starting stacking utility...')

    task_func = partial(do_stack_task, config)
    process_func = partial(process_result, index) if config['index_datasets'] else None
    task_app.run_tasks(tasks, executor, task_func, process_func, queue_size)


if __name__ == '__main__':
    main()
