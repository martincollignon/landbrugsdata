SOURCES = {
    "agricultural_fields": {
        "name": "Danish Agricultural Fields",
        "type": "wfs",
        "description": "Weekly updated agricultural field data",
        "url": "https://geodata.fvm.dk/geoserver/wfs",
        "layer": "Marker:Marker_seneste",
        "frequency": "weekly",
        "enabled": True
    },
    "wetlands": {
        "name": "Danish Wetlands Map",
        "type": "shapefile",
        "description": "Wetland areas from Danish EPA",
        "filename": "kulstof2022",
        "frequency": "static",
        "enabled": True,
        "url": "https://wfs2-miljoegis.mim.dk/natur/wfs",
        "layer": "natur:kulstof2022"
    },
        "cadastral": {
        "name": "Danish Cadastral Properties",
        "type": "wfs",
        "description": "Current real estate property boundaries",
        "url": "https://services.datafordeler.dk/MATRIKLEN2/MatGaeldendeOgForeloebigWFS/1.0.0/Wfs",
        "frequency": "weekly",
        "enabled": True
    }
}
