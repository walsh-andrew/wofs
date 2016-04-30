"""
tests/transform_coord.py
149.16032 -35.34236
==>Transformed to ==>
1552370.25991 -3964606.702
None
1500000 -4000000
==>Transformed to ==>
148.638689676 -35.7203183267
long lat index for the GA pond:
2094.81039627 1415.73192006

"""

from pyproj import Proj, transform


def test0():
    inProj = Proj(init='epsg:3857')
    outProj = Proj(init='epsg:4326')
    x1, y1 = -11705274.6374, 4826473.6922
    x2, y2 = transform(inProj, outProj, x1, y1)
    print x2, y2


def test1():
    """from Geographic (long, lat) to Aussie Albers Conic"""

    print "test1() begins:"
    inProj = Proj(init='epsg:4326')

    # GA water pond
    x1 = 149.16032  # long
    y1 = -35.34236  # lat

    outProj = Proj(init='epsg:3577')
    # x1,y1 = 1500000,-4000000

    x2, y2 = transform(inProj, outProj, x1, y1)

    print x1, y1
    print "==>Transformed to ==>"
    print x2, y2

    return [(x1, y1), (x2, y2)]


def test2():
    """from Albers to Geographic"""

    print "test2() begins:"

    inProj = Proj(init='epsg:3577')
    x1, y1 = 1500000, -4000000  # agdc-v2 tile_index=(15,-40), appear to be low left corner

    outProj = Proj(init='epsg:4326')
    x2, y2 = transform(inProj, outProj, x1, y1)

    print x1, y1
    print "==>Transformed to ==>"
    print x2, y2

    return [(x1, y1), (x2, y2)]

def gdalogr_way():
    import os
    from osgeo import ogr
    from osgeo import osr

    os.environ['GDAL_DATA']='/Softdata/anaconda250/share/gdal/'

    source = osr.SpatialReference()
    source.ImportFromEPSG(3577)

    target = osr.SpatialReference()
    target.ImportFromEPSG(4326)

    transform = osr.CoordinateTransformation(source, target)

    point = ogr.CreateGeometryFromWkt("POINT (1500000.00, -4000000.00)")
    point.Transform(transform)

    print point.GetX(), point.GetY()

    #print point.ExportToWkt()


def gdalbind():
    """works OK"""

    import ogr, osr,os
    os.environ['GDAL_DATA']='/Softdata/anaconda250/share/gdal/'

    pointX = -11705274.6374
    pointY = 4826473.6922

    pointX, pointY = 1500000, -4000000

    # Spatial Reference System
    inputEPSG = 3577 #3857
    outputEPSG = 4326

    # create a geometry from coordinates
    point = ogr.Geometry(ogr.wkbPoint)
    point.AddPoint(pointX, pointY)

    # create coordinate transformation
    inSpatialRef = osr.SpatialReference()
    inSpatialRef.ImportFromEPSG(inputEPSG)

    outSpatialRef = osr.SpatialReference()
    outSpatialRef.ImportFromEPSG(outputEPSG)

    coordTransform = osr.CoordinateTransformation(inSpatialRef, outSpatialRef)

    # transform point
    point.Transform(coordTransform)

    # print point in EPSG 4326
    print point.GetX(), point.GetY()
#####################################################
if __name__ == "__main__":

    print "main() begins....... "
    pnts1 = test1()

    pnts2 = test2()

    long_diff = (pnts1[1][0] - pnts2[0][0]) / 25
    lat_diff = (pnts1[1][1] - pnts2[0][1]) / 25

    print "##########################"
    print "the GA pond (long,lat) index in the raster"
    print (long_diff, lat_diff)


    print "##########################"
    #gdalogr_way() #does not work   ERROR 5: OGR Error: Corrupt data

    print "##########################"
    gdalbind()