"""
Copied from the NDVI/FC template, this is a quick and dirty approach to
producing a datacube application with full compatibility.
"""

from __future__ import absolute_import, print_function

import copy
import errno
import itertools
import json
import logging
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import time
import numpy as np
import click
import xarray
from pandas import to_datetime

import datacube
import datacube.model.utils
from datacube.ui import click as ui

from datacube.compat import integer_types
from datacube.model import Range
from datacube.ui.task_app import task_app, task_app_options, check_existing_files
from datacube.utils.geometry import unary_union, unary_intersection, CRS
from wofs import wofls

_LOG = logging.getLogger(__name__)

INPUT_SOURCES = [{'nbar': 'ls5_nbart_albers',
                  'pq': 'ls5_pq_legacy_scene',
                  'sensor_name': 'TM',
                  'platform_name': 'LANDSAT-5',
                  'platform_name_short': 'ls5',
                  'source_product': 'ls5_level1_scene'},
                 {'nbar': 'ls7_nbart_albers',
                  'pq': 'ls7_pq_legacy_scene',
                  'sensor_name': 'ETM',
                  'platform_name': 'LANDSAT-7',
                  'platform_name_short': 'ls7',
                  'source_product': 'ls7_level1_scene'},
                 {'nbar': 'ls8_nbart_albers',
                  'pq': 'ls8_pq_legacy_scene',
                  'sensor_name': 'OLI',
                  'platform_name': 'LANDSAT-8',
                  'platform_name_short': 'ls8',
                  'source_product': 'ls8_level1_scene'},
                 ]


# http://gcmdservices.gsfc.nasa.gov/static/kms/platforms/platforms.csv


def get_product(index, definition, dry_run=False, skip_indexing=False):
    """
    Get the database record corresponding to the given product definition
    """
    metadata_type = index.metadata_types.get_by_name(definition['metadata_type'])
    prototype = datacube.model.DatasetType(metadata_type, definition)

    if not dry_run and not skip_indexing:
        prototype = index.products.add(prototype)  # idempotent operation

    return prototype


def make_wofs_config(index, config, dry_run=False, **query):
    """
    Refine the configuration

    The task-app machinery loads a config file, from a path specified on the
    command line, into a dict. This function is an opportunity, with access to
    the datacube index, to modify that dict before it is used (being passed on
    to both the make-tasks and the do-task). If using the save-tasks option,
    the modified config is included in the task file.

    For a dry run, we still need to create a dummy DatasetType to
    generate tasks (e.g. via the GridSpec), but a normal run must index
    it as a product in the database and replace the dummy with a fully-fleshed
    DatasetType since the tasks involve writing metadata to file that
    is specific to the database instance (note, this may change in future).
    """

    if not dry_run:
        _LOG.info('Created DatasetType %s', config['product_definition']['name'])  # true? nyet.

    config['wofs_dataset_type'] = get_product(index, config['product_definition'])

    if not os.access(config['location'], os.W_OK):
        _LOG.warning('Current user appears not have write access output location: %s', config['location'])

    return config


def get_filename(config, x, y, t):
    destination = config['location']
    filename_template = config['file_path_template']

    filename = filename_template.format(tile_index=(x, y),
                                        start_time=to_datetime(t).strftime('%Y%m%d%H%M%S%f'),
                                        version=config['task_timestamp'])  # A per JOB timestamp, seconds since epoch
    return Path(destination, filename)


def group_tiles_by_cells(tile_index_list, cell_index_list):
    key_map = defaultdict(list)
    for x, y, t in tile_index_list:
        if (x, y) in cell_index_list:
            key_map[(x, y)].append((x, y, t))
    return key_map


def generate_tasks(index, config, time, extent=None):
    """
    Yield tasks (loadables (nbar,ps,dsm) + output targets), for dispatch to workers.

    This function is the equivalent of an SQL join query,
    and is required as a workaround for datacube API abstraction layering.
    """
    extent = extent if extent is not None else {}
    product = config['wofs_dataset_type']

    assert product.grid_spec.crs == CRS('EPSG:3577')
    assert all((abs(r) == 25) for r in product.grid_spec.resolution)  # ensure approx. 25 metre raster
    pq_padding = [3 * 25] * 2  # for 3 pixel cloud dilation
    terrain_padding = [6850] * 2
    # Worst case shadow: max prominence (Kosciuszko) at lowest solar declination (min incidence minus slope threshold)
    # with snapping to pixel edges to avoid API questions
    # e.g. 2230 metres / math.tan(math.radians(30-12)) // 25 * 25 == 6850

    gw = datacube.api.GridWorkflow(index, grid_spec=product.grid_spec)  # GridSpec from product definition

    wofls_loadables = gw.list_tiles(product=product.name, time=time, **extent)
    dsm_loadables = gw.list_cells(product='dsm1sv10', tile_buffer=terrain_padding, **extent)

    for input_source in INPUT_SOURCES:
        gqa_filter = dict(product=input_source['source_product'], time=time, gqa_iterative_mean_xy=(0,1))
        nbar_loadables = gw.list_tiles(product=input_source['nbar'], time=time, source_filter=gqa_filter, **extent)
        pq_loadables = gw.list_tiles(product=input_source['pq'], time=time, tile_buffer=pq_padding, **extent)

        # only valid where EO, PQ and DSM are *all* available (and WOFL isn't yet)
        tile_index_set = (set(nbar_loadables) & set(pq_loadables)) - set(wofls_loadables)
        key_map = group_tiles_by_cells(tile_index_set, dsm_loadables)

        # Cell index is X,Y, tile_index is X,Y,T
        for cell_index, tile_indexes in key_map.items():
            geobox = gw.grid_spec.tile_geobox(cell_index)
            dsm_tile = gw.update_tile_lineage(dsm_loadables[cell_index])
            for tile_index in tile_indexes:
                nbar_tile = gw.update_tile_lineage(nbar_loadables.pop(tile_index))
                pq_tile = gw.update_tile_lineage(pq_loadables.pop(tile_index))
                valid_region = find_valid_data_region(geobox, nbar_tile, pq_tile, dsm_tile)
                if not valid_region.is_empty:
                    yield dict(source_tile=nbar_tile,
                               pq_tile=pq_tile,
                               dsm_tile=dsm_tile,
                               file_path=get_filename(config, *tile_index),
                               tile_index=tile_index,
                               extra_global_attributes=dict(platform=input_source['platform_name'],
                                                            instrument=input_source['sensor_name']),
                               valid_region=valid_region)


def make_wofs_tasks(index, config, year=None, **kwargs):
    """
    Generate an iterable of 'tasks', matching the provided filter parameters.

    Tasks can be generated for:

     - all of time
     - 1 particular year
     - a range of years

    Tasks can also be restricted to a given spatial region, specified in `kwargs['x']` and `kwargs['y']` in `EPSG:3577`.
    """
    # TODO: Filter query to valid options
    time = None

    if isinstance(year, integer_types):
        time = Range(datetime(year=year, month=1, day=1), datetime(year=year + 1, month=1, day=1))
    elif isinstance(year, tuple):
        time = Range(datetime(year=year[0], month=1, day=1), datetime(year=year[1] + 1, month=1, day=1))

    extent = {}
    if 'x' in kwargs and kwargs['x']:
        extent['crs'] = 'EPSG:3577'
        extent['x'] = kwargs['x']
        extent['y'] = kwargs['y']

    tasks = generate_tasks(index, config, time=time, extent=extent)
    return tasks


def get_app_metadata(config):
    doc = {
        'lineage': {
            'algorithm': {
                'name': 'datacube-wofs',
                'version': config.get('version', 'unknown'),
                'repo_url': 'https://github.com/GeoscienceAustralia/wofs.git',
                'parameters': {'configuration_file': config.get('app_config_file', 'unknown')}
            },
        }
    }
    return doc


def find_valid_data_region(geobox, *sources_list):
    # perform work in CRS of the output tile geobox
    unfused = [[dataset.extent.to_crs(geobox.crs) for dataset in tile.sources.item()]
               for tile in sources_list]
    # fuse the dataset extents within each source tile
    tiles_extents = map(unary_union, unfused)
    # find where (within the output tile) that all prerequisite inputs available
    return unary_intersection([geobox.extent] + list(tiles_extents))
    # downstream should check if this is empty..


def docvariable(agdc_dataset, time):
    """
    Convert datacube dataset to xarray/NetCDF variable
    """
    array = xarray.DataArray([agdc_dataset], coords=[time])
    docarray = datacube.model.utils.datasets_to_doc(array)
    docarray.attrs['units'] = '1'  # unitless (convention)
    return docarray


def do_wofs_task(config, source_tile, pq_tile, dsm_tile, file_path, tile_index, extra_global_attributes, valid_region):
    """
    Load data, run WOFS algorithm, attach metadata, and write output.

    :param dict config: Config object
    :param datacube.api.Tile source_tile: NBAR Tile
    :param datacube.api.Tile pq_tile: Pixel quality Tile
    :param datacube.api.Tile dsm_tile: Digital Surface Model Tile
    :param Path file_path: output file destination
    :param tuple tile_index: Index of the tile

    :return: Dataset objects representing the generated data that can be added to the index
    :rtype: list(datacube.model.Dataset)
    """
    product = config['wofs_dataset_type']
    app_info = get_app_metadata(config)

    if file_path.exists():
        raise OSError(errno.EEXIST, 'Output file already exists', str(file_path))

    # load data
    bands = ['blue', 'green', 'red', 'nir', 'swir1', 'swir2']  # inputs needed from EO data)
    source = datacube.api.GridWorkflow.load(source_tile, measurements=bands)
    pq = datacube.api.GridWorkflow.load(pq_tile)
    dsm = datacube.api.GridWorkflow.load(dsm_tile, resampling='cubic')

    # Core computation
    result = wofls.woffles(*(x.isel(time=0) for x in [source, pq, dsm])).astype(np.int16)

    # Convert 2D DataArray to 3D DataSet
    result = xarray.concat([result], dim=source.time).to_dataset(name='water')

    # add metadata
    result.water.attrs['nodata'] = 1  # lest it default to zero (i.e. clear dry)
    result.water.attrs['units'] = '1'  # unitless (convention)
    result.water.attrs['crs'] = source.crs

    # Attach CRS. Note this is poorly represented in NetCDF-CF
    # (and unrecognised in xarray), likely improved by datacube-API model.
    result.attrs['crs'] = source.crs

    # Provenance tracking
    parent_sources = [ds for tile in [source_tile, pq_tile, dsm_tile] for ds in tile.sources.values[0]]

    # Create indexable record
    new_record = datacube.model.utils.make_dataset(
        product=product,
        sources=parent_sources,
        center_time=result.time.values[0],
        uri=file_path.absolute().as_uri(),
        extent=source_tile.geobox.extent,
        valid_data=valid_region,
        app_info=app_info
    )

    # inherit optional metadata from EO, for future convenience only
    def harvest(what, tile):
        datasets = [ds for source_datasets in tile.sources.values for ds in source_datasets]
        values = [dataset.metadata_doc[what] for dataset in datasets]
        assert all(value == values[0] for value in values)
        return copy.deepcopy(values[0])

    new_record.metadata_doc['platform'] = harvest('platform', source_tile)
    new_record.metadata_doc['instrument'] = harvest('instrument', source_tile)

    # copy metadata record into xarray
    result['dataset'] = docvariable(new_record, result.time)

    global_attributes = config['global_attributes'].copy()
    global_attributes.update(extra_global_attributes)

    # write output
    datacube.storage.storage.write_dataset_to_netcdf(result, file_path,
                                                     global_attributes=global_attributes,
                                                     variable_params=config['variable_params'])
    return [new_record]


def validate_year(ctx, param, value):
    try:
        if value is None:
            return None
        years = list(map(int, value.split('-', 2)))
        if len(years) == 1:
            return years[0]
        return tuple(years)
    except ValueError:
        raise click.BadParameter('year must be specified as a single year (eg 1996) '
                                 'or as an inclusive range (eg 1996-2001)')


APP_NAME = 'wofs'


@click.command(name=APP_NAME)
@ui.pass_index(app_name=APP_NAME)
@click.option('--dry-run', is_flag=True, default=False, help='Check if output files already exist')
@click.option('--year', callback=validate_year, help='Limit the process to a particular year or a range of years')
@click.option('--queue-size', type=click.IntRange(1, 100000), default=3200,
              help='Number of tasks to queue at the start')
@click.option('--print-output-product', is_flag=True)
@click.option('--skip-indexing', is_flag=True, default=False)
#@click.option('--x', nargs=2, type=int) This functionality doesn't work, creates borders on tiles
#@click.option('--y', nargs=2, type=int)
@task_app_options
@task_app(make_config=make_wofs_config, make_tasks=make_wofs_tasks)
def wofs_app(index, config, tasks, executor, dry_run, queue_size,
             print_output_product, skip_indexing, *args, **kwargs):
    if dry_run:
        check_existing_files((task['file_path'] for task in tasks))
        return 0
    else:
        if not skip_indexing:
            # Ensure output product is in index
            config['wofs_dataset_type'] = index.products.add(config['wofs_dataset_type'])  # add is idempotent

    if print_output_product:
        click.echo(json.dumps(config['wofs_dataset_type'].definition, indent=4))
        return 0

    click.echo('Starting processing...')
    results = []

    def submit_task(task):
        _LOG.info('Queuing task: %s', task['tile_index'])
        results.append(executor.submit(do_wofs_task, config=config, **task))

    task_queue = itertools.islice(tasks, queue_size)
    for task in task_queue:
        submit_task(task)
    click.echo('Queue filled, waiting for first result...')

    successful = failed = 0
    while results:
        result, results = executor.next_completed(results, None)

        # submit a new task to replace the one we just finished
        task = next(tasks, None)
        if task:
            submit_task(task)

        # Process the result
        try:
            datasets = executor.result(result)
            for dataset in datasets:
                if not skip_indexing:
                    start = time.clock()
                    index.datasets.add(dataset, sources_policy='skip')
                    indexing_time = time.clock() - start
                    _LOG.info('Dataset added to index in %fs: id=%s path=%s', indexing_time, dataset.id, dataset.local_path)
                else:
                    _LOG.info('Dataset completed: id=%s path=%s', dataset.id, dataset.local_path)
            successful += 1
        except Exception as err:  # pylint: disable=broad-except
            _LOG.exception('Task failed: %s', err)
            failed += 1
            continue
        finally:
            # Release the task to free memory so there is no leak in executor/scheduler/worker process
            executor.release(result)

    click.echo('%d successful, %d failed' % (successful, failed))
    _LOG.info('Completed: %d successful, %d failed', successful, failed)


if __name__ == '__main__':
    wofs_app()
