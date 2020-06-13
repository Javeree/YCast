#!/usr/bin/env python3

import os
import sys
import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import urllib.parse as parse
import xml.etree.cElementTree as etree
import logging
import logging.handlers
import yaml
import urllib3

VTUNER_DNS = 'http://radioyamaha.vtuner.com'
VTUNER_INITURL = '/setupapp/Yamaha/asp/BrowseXML/loginXML.asp'
VTUNER_STATURL = '/setupapp/Yamaha/asp/BrowseXML/statxml.asp'
XMLHEADER = '<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>'
YCAST_LOCATION = 'ycast'
DEFAULTSTATION = 'Radio Paradise - auto:http://stream.radioparadise.com/mp3-192'

logger = logging.getLogger(__name__)
logger.addHandler(logging.StreamHandler())
facility = logging.handlers.SysLogHandler.LOG_LOCAL0
logger.addHandler(logging.handlers.SysLogHandler('/dev/log', facility))
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

def filter_url(url):
    ''' Check the url and translate it if needed into a direct url '''
    if 'streamtheworld.com/' in url:
        http = urllib3.PoolManager()
        resp = http.urlopen('GET', url, redirect=False)
        newurl = resp.get_redirect_location()
        return newurl
    return url


class StationSource():
    def __init__(self, source):
        self.stations = {}
        self.stations_by_id = {}
        if os.path.isfile(source):
            self.source = source
        else:
            ycast_dir = os.path.dirname(os.path.realpath(__file__))
            self.source = ycast_dir + '/stations.yml'

    def get_stations(self):
        try:
            with open(self.source, 'r') as sourcefile:
                self.stations = yaml.load(sourcefile, Loader=yaml.FullLoader)
        except FileNotFoundError:
            logger.error("ERROR: Station configuration not found. Please supply a proper stations.yml.")
            sys.exit(1)
        self.set_station_by_id()
        return self.stations


    def set_station_by_id(self, station_id=1, long_category=None):
        ''' Associate each station with a unique id '''
        def walktree(directory, station_id=1, category=None):
            for key, data in directory.items():
                if isinstance(data, dict):
                    station_id = walktree(data, station_id)
                elif isinstance(data, str):
                    directory[key] = (station_id, data)
                    self.stations_by_id[station_id] = (key, data)
                    station_id += 1
            return station_id
        walktree(self.stations)


    def by_hierarchy(self, long_category):
        ''' Return a dictionary of stations/dirs based on a long category name
            a long category is a string with the hierarchy of categories: 'category|subcategory|subcategor|..'
        '''
        hierarchy = long_category.split('|')
        current_dir = self.stations
        for category in hierarchy:
            current_dir = current_dir[category]
        return current_dir

    def by_id(self, station_id):
        return self.stations_by_id[station_id]


class YCastHandler(BaseHTTPRequestHandler):
    ''' YCastServer creates an instance of this class for each received message. __init__ passes the message to do_GET '''

    def do_GET(self):
        ''' Handle the GET request and send reply to the client '''
        logger.info(f'message received: {self.path}')
        stations = self.server.source.get_stations()
        url_split = parse.urlsplit(self.path)
        url_query_split = parse.parse_qs(url_split.query)
        if url_split.path == VTUNER_INITURL:
            if 'token' in url_query_split:
                # First request on start of the Amplifier
                xml = etree.Element('EncryptedToken')
                xml.text = '85d6fa40a9dcc906'   # any arbitrarytoken
                self.write_message(xml, add_xml_header=False)
            else:
                # A root directory request
                start = int(url_query_split['start'][0])
                size = int(url_query_split['howmany'][0])
                self.reply_with_dir(stations, start - 1, size)

        elif self.path.startswith(VTUNER_STATURL):
            station_id = int(url_query_split['id'][0])
            try:
                station_name, station_url = self.server.source.by_id(station_id)
            except KeyError:
                station_id = 999999
                station_name, _, station_url = DEFAULTSTATION.partition('&')
            xml = self.create_root()
            self.add_station(xml, station_name, station_url, station_id)
            self.write_message(xml)

        elif self.path == '/' \
                or self.path == '/' + YCAST_LOCATION \
                or self.path == '/' + YCAST_LOCATION + '/'\
                or self.path.startswith(VTUNER_INITURL):
            self.reply_with_dir(stations)

        elif self.path.startswith('/' + YCAST_LOCATION + '?'):
            hierarchy = parse.unquote(url_query_split['category'][0])
            try:
                start = int(url_query_split['start'][0])
                size = int(url_query_split['howmany'][0])
            except KeyError:
                start = 1
                size = 8
            try:
                self.reply_with_mixed_list(hierarchy, start - 1, size)
            except KeyError:
                self.send_error(404)
        else:
            self.send_error(404)


    def reply_with_dir(self, stations, start=0, max_size=8):
        ''' Build an xml reply that represents a list of all directories
            stations: the list of items to display
            start: the first element of the list to display
            max_size: the max number of elements to display
        '''
        xml = self.create_root()
        count = etree.SubElement(xml,'DirCount').text = '9'
        for category in sorted(stations, key=str.lower)[start:start+max_size]:
            self.add_dir(xml, category,
                         VTUNER_DNS + '/' + YCAST_LOCATION + '?category=' + parse.quote(category),
                         str(len(stations[category])))
        self.write_message(xml)


    def reply_with_station_list(self, station_list, start=0, max_size=8):
        ''' Build an xml reply that represents a list of all stations
            station_list: the list of items to display
            start: the first element of the list to display
            max_size: the max number of elements to display
        '''
        xml = self.create_root()
        for station in sorted(station_list, key=str.lower)[start:start+max_size]:
            station_id, station_url = station_list[station]
            self.add_station(xml, station, station_url, station_id)
        self.write_message(xml)

    def reply_with_mixed_list(self, hierarchy, start=0, max_size=8):
        ''' Build an xml reply that represents a list of mixed stations/directories
            hierarchy: the list of items to display
            start: the first element of the list to display
            max_size: the max number of elements to display
        '''
        station_list = self.server.source.by_hierarchy(hierarchy)
        xml = self.create_root()
        for item in sorted(station_list, key=str.lower)[start:start+max_size]:
            if isinstance(station_list[item], dict):
                category = hierarchy + '|' + item
                self.add_dir(xml, item,
                             VTUNER_DNS + '/' + YCAST_LOCATION + '?category=' + parse.quote(category),
                             str(len(station_list[item])))
            elif isinstance(station_list[item], tuple):
                station = item
                station_id, station_url = station_list[station]
                self.add_station(xml, station, station_url, station_id)
        self.write_message(xml)


    def write_message(self, xml, add_xml_header=True):
        ''' Write a message containing the given xml to send to the ycast client '''
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        if add_xml_header:
            self.wfile.write(bytes(XMLHEADER, 'utf-8'))
        reply=etree.tostring(xml).decode('utf-8')
        logger.info(f'Sending reply: {reply}')
        self.wfile.write(bytes(reply, 'utf-8'))


    def create_root(self):
        ''' Create the root of an xml tree '''
        return etree.Element('ListOfItems')


    def add_dir(self, root, name, dest, dircount):
        ''' Add a directory entry to the xml node
            root: the node to add the directory information to
            name: the name of the directory
            dest: the url to visit to get the contents of the directory
            dircount: the number of items in the directory (allows the client to say it is showing page 1 of 3)
        '''
        item = etree.SubElement(root, 'Item')
        etree.SubElement(item, 'ItemType').text = 'Dir'
        etree.SubElement(item, 'Title').text = name
        etree.SubElement(item, 'UrlDir').text = dest
        etree.SubElement(item, 'DirCount').text = dircount
        return item


    def add_station(self, root, name, url, station_id):
        ''' Add a station entry to the xml node
            root: the node to add the directory information to
            name: the name of the station
            url: the url to visit to listen to the station
            station_id: the unique id of the station (that will be sent to the server if the client wants it)
        '''
        item = etree.SubElement(root, 'Item')
        etree.SubElement(item, 'ItemType').text = 'Station'
        etree.SubElement(item, 'StationName').text = name
        etree.SubElement(item, 'StationId').text = str(station_id)
        etree.SubElement(item, 'StationUrl').text = filter_url(url)
        return item


class YCastServer(HTTPServer):
    ''' A HTTPServer that retains a pointer to the source to be used by the BaseHTTPRequestHandler
    '''
    def __init__(self, source, *args, **kwargs):
        self.source = StationSource(source)
        address,port = args[0]
        logger.info(f'YCast server listening on {address}:{port}')
        super().__init__(*args, **kwargs)


    def __enter__(self):
        return self


    def __exit__(self, *args):
        logger.info('YCast server shutting down')
        self.server_close()


parser = argparse.ArgumentParser(description='vTuner API emulation')
parser.add_argument('-l', action='store', dest='address', help='Listen address', default='0.0.0.0')
parser.add_argument('-p', action='store', dest='port', type=int, help='Listen port', default=80)
parser.add_argument('-s', action='store', dest='station_list', type=str, help='station list file', default='stations.yml')
arguments = parser.parse_args()
try:
    with YCastServer(arguments.station_list, (arguments.address, arguments.port), YCastHandler) as server:
        print('listening', server)
        server.serve_forever()
except OSError as err:
    logger.error(f'OS reports: \"{err.strerror}\"')
    sys.exit(2)
except PermissionError:
    logger.error("No permission to create socket. Are you trying to use ports below 1024 without elevated rights?")
    sys.exit(1)
except KeyboardInterrupt:
    pass
