from flask import Flask, request, Response
import json
import pyjq
import yaml
import sys
import os
import requests
import copy
import logging
logging.basicConfig(stream=sys.stdout,
                    format='%(asctime)s | %(levelname)-8.8s | %(filename)s | %(process)d | %(message).10000s',
                    datefmt='%Y/%m/%d %H:%M:%S',
                    level=logging.DEBUG if int(os.getenv('FLASK_DEBUG', '0')) else logging.INFO)

CORS_HEADER={'Access-Control-Allow-Origin': '*'}
app = Flask(__name__)
if os.environ['CONFIG'].startswith('@'):
    config = open(os.environ['CONFIG'].lstrip('@')).read()
else:
    config = os.environ['CONFIG']
configuration = yaml.load(config, Loader=yaml.FullLoader)
print("configuration is ")
print(yaml.dump(configuration, sys.stdout))
session = requests.session()
if 'CURL_CA_BUNDLE' in os.environ and not os.getenv('CURL_CA_BUNDLE'):
    session.verify = False

def transform_object(obj, context):
    logging.debug('transforming %s of type %s', obj, type(obj))
    if isinstance(obj, str):
        return obj
    if 'jq' in obj:
        script = obj['jq'] if isinstance(obj['jq'], str) else json.dumps(obj['jq'])
        logging.debug('jq script is %s of type %s', script, type(script))
        try:
            res = pyjq.first(script, context)
            return res if isinstance(res, str) else json.dumps(res)
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
    v = {'env': json.dumps(dict(os.environ)), 'args': json.dumps(dict(request.args)), 'request': request.get_data().decode('utf-8')}
    url = transform_object(upstream_config['url'], context)
    method = upstream_config.get('method', 'get')
    body = transform_object(upstream_config['body'], context) if 'body' in upstream_config else context['request']['body']
    content_type = upstream_config.get('content_type', context['request']['content_type'])
    headers = {x: transform_object(y, context) for x, y in config.get('headers', {}).items()}
    headers['Content-type'] = content_type
    if logging.getLogger().level <= logging.DEBUG:
        logging.debug('context is:')
        json.dump(context, sys.stdout, indent=2)
    logging.debug('url is %s', url)
    logging.debug('method is %s', method)
    logging.debug('headers is %s', headers)
    logging.debug('body is %s', str(body))
    req = requests.Request(method=method, url=url, headers=headers, data=body)
    resp = session.send(req.prepare())
    logging.debug('got status %s from upstream', resp.status_code)
    return resp

def json_try(obj):
    try:
        return(json.loads(obj))
    except json.JSONDecodeError:
        return None

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
        logging.debug('body:')
        logging.debug(str(context['request']['body']))
        context['request']['json'] = json_try(context['request']['body'])
        context['request']['content_type'] = request.content_type
        context['request']['headers'] = dict(request.headers)
        context['request']['method'] = str(request.method)
        if 'upstream' in config:
            resp = get_response(config['upstream'], context)
            context['response'] = {}
            context['response']['body'] = resp.text
            try:
                context['response']['json'] = resp.json()
            except json.JSONDecodeError:
                pass
            context['response']['status_code'] = resp.status_code
            context['response']['content_type'] = resp.headers.get('Content-Type', 'plain/text')
        else:
            body = transform_object(config['value'], context)
            logging.debug('body of response is %s', body)
            context['response'] = {
                    'body': body,
                    'json': json_try(body),
                    'status_code': int(config.get('status_code', 200)),
                    'content_type': config.get('content_type', 'application/json')
            }
        if 'transformations' not in config:
            return Response(context['response']['body'], status=context['response']['status_code'], content_type=context['response']['content_type'], headers=CORS_HEADER)
        return transform(config['transformations'], context)

def transform_if_needed(transformation, prop, context):
    return transform_object(transformation[prop], context) if prop in transformation else context['response'][prop]

def transform(transformations, context):
    code = context['response']['status_code']
    matched_code = code if code in transformations else 'default'
    logging.debug('matching transformation: %s', matched_code)
    transformation = transformations[matched_code]
    body = transform_if_needed(transformation, 'body', context)
    status_code = transform_if_needed(transformation, 'status_code', context)
    content_type = transform_if_needed(transformation, 'content_type', context)
    return Response(body, status=int(status_code), content_type=content_type, headers=CORS_HEADER)
