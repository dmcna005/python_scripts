#!/usr/bin/env python3

# The purpose of this script is to test the Mongo change stream feature.
# See usage information for instructions on how to use it.

import argparse
import json
import pymongo
import sys


def main(
        host=None,
        port=None,
        database=None,
        collection=None,
        pipeline=None,
    ):
    pipeline = json.loads(pipeline)
    print(
        'Connecting to {host}:{port}/{database}, collection {collection}, pipeline {pipeline}.'.format(
            host=host,
            port=port,
            database=database,
            collection=collection,
            pipeline=pipeline,
        ),
        file=sys.stderr,
    )
    change_stream = (pymongo
        .MongoClient(host, port)
        [database]
        [collection]
        .watch(
            pipeline=pipeline,
            full_document='updateLookup',
        )
    )
    print('Change stream initialized.', file=sys.stderr)
    for change_event in change_stream:
        print(change_event)


if __name__ == '__main__':
    help = '''
        This program works by opening a change stream cursor on MongoDB as specified by parameters
        and outputting results of the cursor on STDOUT until the cursor terminates or the program
        is halted.  Additional status of the program is output on STDERR.
        Program can be halted by pressing Ctrl+C  or piping to a finite output process like head.
    '''
    defaults = {
        'host': 'localhost',
        'port': 27017,
        'database': 'sailthru',
        'collection': 'client',
        'pipeline': '[{"$match": {"fullDocument.client_id": 3386}}]'
    }
    gen_help = lambda default_key: 'Defaults to {default_key}'.format(default_key=defaults.get(default_key))
    arg_parser = argparse.ArgumentParser(description=help)
    arg_parser.add_argument('-H', '--host', type=str, default=defaults['host'], help=gen_help('host'))
    arg_parser.add_argument('-p', '--port', type=int, default=defaults['port'], help=gen_help('port'))
    arg_parser.add_argument('-d', '--database', type=str, default=defaults['database'], help=gen_help('database'))
    arg_parser.add_argument('-c', '--collection', type=str, default=defaults['collection'], help=gen_help('collection'))
    arg_parser.add_argument('--pipeline', type=str, default=defaults['pipeline'], help=gen_help('pipeline'))
    main(**vars(arg_parser.parse_args()))
