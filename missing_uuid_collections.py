#!/usr/bin/env python3

import pymongo
from subprocess import Popen, PIPE, STDOUT

# the cluster you want to inspect. 17020 is profile.
mc = pymongo.MongoClient("mongodb://nj1-badcotton.flt:17022/?readPreference=secondary")

# All collections in config.collections are sharded.
for d in mc.config.databases.find({'_id': {'$ne': 'admin'}}):
    database = d['_id']
    for c in mc.config.collections.find({'dropped': False}):
        ns = c['_id']
        db, collection = ns.split('.', 1)
        # collection = ns.split('.')[-1]
        # print(collection)
        if database == 'config':
            continue

        # Get the primary shard for the collection's database.
        database_doc = mc.config.databases.find_one({'_id': database})
        if not database_doc:
            continue
        primary_shard = database_doc['primary']

        # Construct a URI to connect to the primary shard replica set.
        shard_doc = mc.config.shards.find_one({'_id': primary_shard})
        rs_hosts = shard_doc['host'].split('/')[1]
        rs_uri = f"mongodb://{rs_hosts}/?replicaSet={primary_shard}"
        #print(rs_uri)

        # Ask the primary shard replicaset which mongod is the primary
        mc_shard = pymongo.MongoClient(rs_uri)
        status = mc_shard.admin.command('replSetGetStatus')
        members = status['members']
        rs_primary_hostport = [m['name'] for m in members if m['stateStr'] == 'PRIMARY'][0]
        #print(rs_primary_hostport)

    	# connect to the primary mongod, see if it has a cache entry and if so whether it has a uuid and if it does not then delte cache entry and drop the cache
        mc_shard_primary = pymongo.MongoClient(f"mongodb://{rs_primary_hostport}/")
        cache_doc = mc_shard_primary.config['cache.collections'].find_one({'_id': ns})
        cache_col = mc_shard_primary.config['cache.chunks'].ns
        if cache_doc:
            status = 'UUID_MISSING' if 'uuid' not in cache_doc else 'OK'
        else:
            status = 'NO_CACHE_DOC_TO_REPORT'


        # if status == 'UUID_MISSING':
        #     # print(mc_shard_primary.config['cache.collections'].delete_one({'_id': ns}))
        #     # print(cache_col.drop())
        #     docs = mc.database.collection.find()
        #     doc_list = list(docs)
        #     doc_len = len(doc_list)
        #     print(doc_list, doc_len)
        # else:
        print(ns, primary_shard, rs_primary_hostport, status, )
