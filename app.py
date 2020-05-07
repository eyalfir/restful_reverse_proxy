from flask import Flask, request, Response
import json
import pyjq
import yaml
import sys
import os
import requests
import copy
from envsubst import envsubst
import logging
logging.basicConfig(stream=sys.stdout,
                    format='%(asctime)s | %(levelname)-8.8s | %(filename)s | %(process)d | %(message).10000s',
                    datefmt='%Y/%m/%d %H:%M:%S',
                    level=logging.DEBUG if int(os.getenv('FLASK_DEBUG', '')) else INFO)

CORS_HEADER={'Access-Control-Allow-Origin': '*'}
app = Flask(__name__)
configuration_transformation = envsubst if os.getenv('ENVSUBST') else lambda x: x
configuration = yaml.load(configuration_transformation(open(os.environ['CONFIG']).read()), Loader=yaml.FullLoader)
print("configuration is ")
print(yaml.dump(configuration, sys.stdout))
session = requests.session()
if 'CURL_CA_BUNDLE' in os.environ and not os.getenv('CURL_CA_BUNDLE'):
    session.verify = False

def transform_object(obj, context):
    logging.debug('transforming %s of type %s', obj, type(obj))
    if isinstance(obj, str):
        return var
    elif 'jq' in obj:
        script = obj['jq'] if isinstance(obj['jq'], str) else json.dumps(obj['jq'])
        logging.debug('jq script is %s of type %s', script, type(script))
        try:
            res = pyjq.first(script, context)
            if isinstance(res, str):
                return res
            else:
                return json.dumps(res)
        except pyjq._pyjq.ScriptRuntimeError as e:
            logging.error('cannot transform:')
            logging.error('object is %s', repr(obj['jq']))
            logging.error('context is %s', repr(context))
            logging.exception(str(e))
            sys.exit(1)
    else:
        raise Exception("unknown transformation: %s" % repr(obj))

def get_response(upstream_config, context):
    config = upstream_config
    if 'value' in config:
        return Response(config['value'], status=200, content_type='application/json', headers=CORS_HEADER)
    v = {'env': json.dumps(dict(os.environ)), 'args': json.dumps(dict(request.args)), 'request': request.get_data().decode('utf-8')}
    url = transform_object(upstream_config['url'], context)
    method = upstream_config.get('method', 'get')
    headers = {x: transform_object(y, context) for x, y in config.get('headers', {}).items()}
    logging.debug('url is %s', url)
    logging.debug('method is %s', method)
    logging.debug('headers is %s', headers)
    req = requests.Request(method=method, url=url, headers=headers)
    resp = session.send(req.prepare())
    logging.debug('got status %s from upstream', resp.status_code)
    return resp

def json_try(obj):
    try:
        return(json.loads(obj))
    except json.JSONDecodeError:
        return null

for route in configuration:
    @app.route(route['path'], endpoint=route['path'], methods=route.get('methods', ['get']))
    def handle(config=route, **kwargs):
        logging.debug('got request for route %s with args %s', request.path, request.args)
        context = {}
        context['env'] = dict(os.environ)
        context['request'] = {}
        context['request']['args'] = dict(request.args)
        context['request']['args'].update(kwargs)
        context['request']['body'] = request.get_data().decode('utf-8')
        context['request']['json'] = json_try(context['request']['body'])
        context['request']['method'] = str(request.method)
        if 'upstream' in config:
            resp = get_response(config['upstream'], context)
            context['response'] = {}
            context['response']['body'] = resp.text
            context['response']['json'] = resp.json()
            context['response']['status_code'] = resp.status_code
            context['response']['content_type'] = resp.headers.get('Content-Type', 'plain/text')
        else:
            body = transform_object(config['value'], context)
            logging.debug('body of response is %s', body)
            context['response'] = {
                    'body': body,
                    'json': json_try(body),
                    'status_code': 200,
                    'content_type': 'application/json'
            }
        if 'transformations' not in config:
            return Response(context['response']['body'], status=context['response']['status_code'], content_type=context['response']['content_type'], headers=CORS_HEADER)
        return transform(config['transformations'], context)

    def transform(transformations, context):
        code = context['response']['status_code']
        matched_code = code if code in transformations else 'default'
        logging.debug('matching transformation: %s', matched_code)
        transformation = transformations[matched_code]
        body = transform_object(transformation['body'], context) if 'body' in transformation else context['response']['body']
        status = transform_object(transformation['status_code'], context) if 'status_code' in transformation else context['response']['status_code']
        return Response(body, status=int(status), content_type='application/json', headers=CORS_HEADER)
