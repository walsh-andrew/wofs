#################################################
# Purpose: Test use agdc-v2 api to retrieve tiles data and test a water classifier algorithm
#
# Retrieve/load tiles data_array for nbar, pq, and dsm; apply water classification algorithm to produce water tiles
# Output:  water tiles written in to  /g/data/u46/fxz547/wofs2/extents/abccellid/*.nc
#
# Usage:
#   export PYTHONPATH=/g/data/u46/fxz547/Githubz/wofs/:/g/data/u46/fxz547/Githubz/agdc-v2
#   python make_water_tiles.py
#################################################

import os, sys
from collections import defaultdict

import numpy
from scipy import stats
import xarray as xr
import xarray.ufuncs

import datacube
from datacube.api import GridWorkflow
from datacube.index import index_connect
from datacube.config import LocalConfig
#from datacube.api._conversion import to_datetime
from datacube.api import make_mask #, masking

from wofs.waters.detree.classifier import WaterClassifier
import wofs.waters.detree.filters as filters
from wofs.workflow.agdc_dao import AgdcDao
import wofs.waters.WaterConstants as WaterConstants


import logging

logging.basicConfig(level=logging.INFO)

# ------------------------------------------------------------
def comput_img_stats(waterimg):
    """
    compute and show the pixel values stats of a 1-band image, typically water extent
    :param waterimg: 2D numpy array
    :return:
    """

    print waterimg.shape


    water_pix = numpy.sum(waterimg == WaterConstants.WATER_PRESENT)  # water=128
    cloud_pix= numpy.sum(waterimg == WaterConstants.MASKED_CLOUD)   # 64
    cloudshadow_pix= numpy.sum(waterimg == WaterConstants.MASKED_CLOUD_SHADOW) #32
    highslope_pix=numpy.sum(waterimg == WaterConstants.MASKED_HIGH_SLOPE) #16
    terrainshadow_pix=numpy.sum(waterimg == WaterConstants.MASKED_TERRAIN_SHADOW) #8
    sea_pix= numpy.sum(waterimg == WaterConstants.MASKED_SEA_WATER)  #4
    noncontig_pix= numpy.sum(waterimg == WaterConstants.MASKED_NO_CONTIGUITY) #2
    nodata_pix = numpy.sum(waterimg == WaterConstants.NO_DATA)  # water_extent nodata==1
    nowater_pix = numpy.sum(waterimg == WaterConstants.WATER_NOT_PRESENT)  # not water =0

    print water_pix,cloud_pix, cloudshadow_pix, noncontig_pix, nodata_pix,  nowater_pix

    total_pix = water_pix + cloud_pix +  cloudshadow_pix+ noncontig_pix + nodata_pix + nowater_pix
    print ('Total number of pixel should be 16M ',total_pix)

    wimg1d = waterimg.flat

    print stats.describe(wimg1d)

    return

# ------------------------------------------------------------
def write_img(waterimg, geometa, path2file):
    """
     write output the numpy array waterimg into a netcdf file?
    :param waterimg:
    :param geometa:

    :param path2file:
    :return:
    """
    xrarr = xr.DataArray(waterimg, name=geometa["name"])

    xrds = xrarr.to_dataset()
    xrds.to_netcdf(path2file)

    return path2file


def define_water_fname(platform, cellindex, dtstamp, nbar_tile=None):
    """
    define a proper water file name from nbar_tile dataset or xarrray? which contain platform, dtstamp
    :param nbar_tile:
    :return:
    """
    import datetime

    cellid_str = "%s_%s" % (cellindex[0],cellindex[1])
    celldir = "abc%s" % (cellid_str)  # acell's dirname in wofs/extents/

    #timestamp = to_datetime(t).isoformat()[:-6]  # remove the trail +00:00, get a str like "2013-04-11T23:46:35.385577"
    #fakeit: dtstamp = datetime.datetime.utcnow().isoformat()

    print ("DateTime of satellite observation: ", dtstamp)

    dtstamp = dtstamp.replace(":", "-")
    outfilename = "%s_water_%s_%s.nc" % (platform, cellid_str, dtstamp)
    #target look like: LANDSAT_8_water_15_-40_2013-04-11T23:46:35.385577.nc

    EXTENTS_ROOT_DIR="/g/data1/u46/fxz547/wofs2/extents"

    path2waterfile = os.path.join(EXTENTS_ROOT_DIR, celldir, outfilename)
    print path2waterfile

    # water_classified_img.tofile(path2outf) #raw data numpy file

    return path2waterfile

def get_no_data_mask(nbar_data):
    """
    get a mask of where the nodata is set in any of the bands.
    The code must change if the AGDC-v2 API changes
    :param nbar_data: a 6-bands nbar tile
    :return: no_data_mask  xarray 3D [1,4000,4000]
    """

    # Get the nodata values (as an 6 elem array)for each of the 6 bands (in case they aren't the same)
    no_data_values = nbar_data.apply(lambda data_array: data_array.nodata).to_array(dim='band')

    # Turn the Dataset into a DataArray, so we can check all bands
    stack = nbar_data.to_array(dim='band')

    # Find all values that are set to no data, from any band
    no_data_mask = (stack == no_data_values).any(dim='band')

    logging.debug(type(no_data_mask))

    #no_data_mask.plot()

    return no_data_mask


def produce_water_tile(nbar_tile, pq_tile, dsm_tile=None):
    """
    Apply a water classifier algorithm and relevant filters, to produce a water tile.
    As a benchmark, this function intends to re-implement the wofs-v1 algorithm and workflow steps.
    new algorithm and workflow will be developed later.

    Inputs: 2D arrays nbar-pq pair tiles and dsm tile,
    :param nbar_tile:
    :param pq_tile:
    :param dsm_tile:

    :return: water_image
    """
    
    # have to massage the input datasets nbar_tile, pq_tile into suitable for classifiers:
    #get the nbar_tile shape here
    y_size=4000
    x_size=4000

    raw_image = numpy.zeros((6, y_size, x_size), dtype='int16')  #'float32')

    raw_image[0,:,:] = nbar_tile.blue[:,:]
    raw_image[1,:,:] = nbar_tile.green[:,:]
    raw_image[2,:,:] = nbar_tile.red[:,:]
    raw_image[3,:,:] = nbar_tile.nir[:,:]
    raw_image[4,:,:] = nbar_tile.swir1[:,:]
    raw_image[5,:,:] = nbar_tile.swir2[:,:]

    classifier = WaterClassifier()

    # water classification using the input nbar data tiles
    # 1. raw water extent
    water_classified_img = classifier.classify(raw_image)
    del raw_image

    #2 moved down

    # 3 Non-Contiguity, where 1 or more bands had problem
    # with rio.open(self.pq_path) as pq_ds:   pq_band = pq_ds.read(1)
    #
    # water_band = filters.ContiguityFilter(pq_band).apply(water_band)
    # # write_img(water_band, self.output().path, fmt=GTIFF, geobox=geobox, compress='lzw')
    no_contig_mask = make_mask(pq_tile, contiguous=False).pixelquality.values[0]

    water_classified_img[no_contig_mask] = WaterConstants.MASKED_NO_CONTIGUITY  # set the non_contig pixels =2

    # comput_img_stats(water_classified_img)

    # 4
    # water_band = filters.CloudAndCloudShadowFilter(pq_band).apply(water_band)  # compare with scratch/cellid/files
    cloud_mask=make_mask(pq_tile, cloud_acca='cloud', cloud_fmask='cloud',contiguous=True).pixelquality.values[0]
    water_classified_img[cloud_mask] = WaterConstants.MASKED_CLOUD

    cloudshad_mask = make_mask(pq_tile, cloud_shadow_acca='cloud_shadow', cloud_shadow_fmask='cloud_shadow'
                               ,contiguous=True).pixelquality.values[0]

    water_classified_img[cloudshad_mask] = WaterConstants.MASKED_CLOUD_SHADOW


    # 2 Nodata filter, where nbar pixels are outside scene. null (value -999)
    # water_classified_img = filters.NoDataFilter().apply(water_classified_img, nbar_tile.values, nodata_val)
    print("no_data value=", nbar_tile.green.nodata)
    #no_data_mask = (nbar_tile.green[:,:] == nbar_tile.green.nodata).to_masked_array()
    no_data_mask = get_no_data_mask(nbar_tile)
    total_no_data_pixel=numpy.sum(no_data_mask == True)
    print ('no data pixels: ',  total_no_data_pixel)
    print no_data_mask.shape  #(1,4000,4000)

    # Apply no data pixel mask here to override pixels masked before as cloud, cloud-shadow, or non-contiguity.
    water_classified_img [no_data_mask.values[0]] =1  # set the no_data pixels of the water_band as 1
    del no_data_mask

    # # TODO: Combined SolarIncidentAngle, TerrainShadow, HighSlope Masks. They all use database DSM tiles.
    # # Computationally expensive and re-projection required.
    # # 5 SIA #6 TerrainShadow #7 HighSlope
    #
    # # TODO: water_band=SolarTerrainShadowSlope(self.dsm_path).filter(water_band)
    #
    # # 8 Land-Sea. This is the last Filter mask out the Sea pixels as flagged in PQ band
    # # using the pq_band read in step- 3 and 4
    #
    # water_band = filters.SeaWaterFilter(pq_band).apply(water_band)
    # comput_img_stats(water_classified_img)

    #
    # print ("Verify the water classified image ")
    #
    # # verify that classified image is a 2D (4000X4000) 1 band image with values in defined domain {0,1,..., 128, }

    comput_img_stats(water_classified_img)

    #  save the image to a file: numpy data
    # https://www.google.com.au/webhp?sourceid=chrome-instant&ion=1&espv=2&ie=UTF-8#q=write%20numpy%20ndarray%20to%20file

    #     no_data_img = (~xr.ufuncs.isfinite(nbar)).any(dim='variable')
    #
    # print "no_data_img type: " + str(type(no_data_img))
    #
    # import matplotlib.pyplot as pyplt
    # no_data_img.plot.imshow()
    # pyplt.show()
    #
    # print "end of program main"

    return water_classified_img


############################################################################################################
#  Note:
# the WOFS-V1 Water Tiles name pattern: water_extent filename: Platform_Sensor_WATER_CELLID_DATETIMESTAMP.tif
# eg
# LS5_TM_WATER_136_-032_1987-12-07T00-12-56.014088.tif
# LS7_ETM_WATER_136_-032_2000-03-05T00-37-07.703569.tif
# LS8_OLI_TIRS_WATER_136_-032_2013-04-11T00-42-50.tif
# ----------------------------------------------------------


########################################################################################################################
# Usage:
#   export PYTHONPATH=/g/data/u46/fxz547/Githubz/wofs/:/g/data/u46/fxz547/Githubz/agdc-v2
#   python make_water_tiles.py
# ----------------------------------------------------------------------------------------
if __name__ == "__main__":

    # First, let's prepare to get some (nbar,pq) pair, and dsm tiles real data
    # cellindex = (15, -41)
    cellindex = (15, -40)

    # OK: qdict={'latitude': (-36.0, -35.0), 'platform': ['LANDSAT_5', 'LANDSAT_7', 'LANDSAT_8'], 'longitude': (149.01, 150.1), 'time': ('1990-01-01', '2016-03-31')}

    # OK qdict={'latitude': (-36.0, -35.0), 'platform': ['LANDSAT_5'], 'longitude': (149.01, 155.1), 'time': ('1990-01-01', '1990-03-31')}
    # OK qdict={'platform': ['LANDSAT_5'],  'time': ('1990-01-01', '1991-03-31')}
    qdict = {'platform': ['LANDSAT_5'], 'time': ('1990-03-01', '1990-12-31')}


    dcdao = AgdcDao()

    #tile_data = dcdao.get_nbarpq_data(cellindex, qdict)

    nbar_pq_data = dcdao.get_multi_nbarpq_tiledata(cellindex, qdict,maxtiles=20)
    # qdict as argument is too generic here.
    # should be more specific, able to retrieve using eg, ((15, -40), numpy.datetime64('1992-09-16T09:12:23.500000000+1000'))

    # TODO: get DSM data for this cell
    dsm_data = dcdao.get_dsm_data(cellindex, {})

    print("Number of (nbar,pqa) tile-pairs:", len(nbar_pq_data))

    # Now ready to apply classification algorithm to the data tiles retrieved.

    icounter = 0

    for (celltime_key, nbar_tile, pq_tile) in nbar_pq_data:
        
        print celltime_key
        cellindex_tup=celltime_key[0]
        acq_dt=celltime_key[1]
        dtstamp = str(acq_dt)[:19].replace(':','-')
        platform='LS5'  #get from the nbar data?


        water_classified_img = produce_water_tile(nbar_tile, pq_tile, dsm_data[cellindex_tup])

        geometadat = {"name": "waterextent", "ablersgrid_cellindex": cellindex}

        path2_waterfile = define_water_fname(platform, cellindex_tup, dtstamp, nbar_tile)

        write_img(water_classified_img, geometadat, path2_waterfile)

        icounter += 1
