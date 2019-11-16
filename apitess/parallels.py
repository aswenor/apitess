"""The family of /parallels/ endpoints"""
import gzip
import os
import queue
import uuid

from bson.objectid import ObjectId
import flask

import tesserae.db.entities
from tesserae.matchers.sparse_encoding import SparseMatrixSearch
import apitess.errors

bp = flask.Blueprint('parallels', __name__, url_prefix='/parallels')


def _validate_units(specs, name):
    """Provide error messages if units are not specified correctly

    Parameters
    ----------
    specs : dict
        specification of which units from what text
    name : str
        either 'source' or 'target'

    Returns
    -------
    list of str
        error messages corresponding to errors encountered
    """
    result = []
    if 'object_id' not in specs:
        result.append('{} is missing object_id.'.format(name))
    if 'units' not in specs:
        result.append('{} is missing units.'.format(name))
    else:
        units = specs['units']
        if units != 'line' and units != 'phrase':
            result.append('{} has unrecognized units: {}'.format(name, units))
    return result


@bp.route('/', methods=('POST',))
def submit_search():
    """Run a Tesserae search"""
    received = flask.request.get_json()
    requireds = {'source', 'target', 'method'}
    missing = []
    for req in requireds:
        if req not in received:
            missing.append(req)
    if missing:
        return apitess.errors.error(
            400,
            data=received,
            message='The request data payload is missing the following required key(s): {}'.format(', '.join(missing)))

    source = received['source']
    target = received['target']

    errors = _validate_units(source, 'source')
    errors.extend(_validate_units(target, 'target'))
    if errors:
        return apitess.errors.error(
            400,
            data=received,
            message='The following errors were found in source and target unit specifications:\n{}'.format('\n\t'.join(errors)))

    source_object_id = source['object_id']
    target_object_id = target['object_id']
    results = flask.g.db.find(tesserae.db.entities.Text.collection,
            _id=[ObjectId(source_object_id), ObjectId(target_object_id)])
    results = {str(t.id): t for t in results}
    errors = []
    if source_object_id not in results:
        errors.append(source_object_id)
    if target_object_id not in results:
        errors.append(target_object_id)
    if errors:
        return apitess.errors.error(
            400,
            data=received,
            message='Unable to find the following object_id(s) among the texts in the database:\n\t{}'.format('\n\t'.join(errors)))
    source_text = results[source_object_id]
    target_text = results[target_object_id]

    method_requireds = {
        'original': {
            'name', 'feature', 'stopwords', 'freq_basis', 'max_distance',
            'distance_basis'
        }
    }
    method = received['method']
    if 'name' not in method:
        return apitess.errors.error(
            400,
            data=received,
            message='No specified method name.')
    missing = []
    for req in method_requireds[method['name']]:
        if req not in method:
            missing.append(req)
    if missing:
        return apitess.errors.error(
            400,
            data=received,
            message='The specified method is missing the following required key(s): {}'.format(', '.join(missing)))

    response = flask.Response()
    response.status_code = 201
    response.status = '201 Created'
    results_id = uuid.uuid4().hex
    # we want the final '/' on the URL
    response.headers['Location'] = os.path.join(bp.url_prefix, results_id, '')

    try:
        flask.g.searcher.queue_search(results_id, method['name'], {
            'texts': [source_text, target_text],
            'unit_type': received['source']['units'],
            'feature': method['feature'],
            'stopwords': method['stopwords'],
            'frequency_basis': method['freq_basis'],
            'max_distance': method['max_distance'],
            'distance_metric': method['distance_basis']
        })
    except queue.Full:
        return apitess.error.error(
            500,
            data=received,
            message=('The search request could not be added to the queue. '
                'Please try again in a few minutes'))
    return response


@bp.route('/status/<results_id>/')
def retrieve_status(results_id):
    results_status_found = flask.g.db.find(
        tesserae.db.entities.ResultsStatus.collection,
        results_id=results_id
    )
    if not results_status_found:
        response = flask.Response('Could not find results_id')
        response.status_code = 404
        return response
    status = results_status_found[0]
    return flask.jsonify(results_id=status.results_id, status=status.status,
            message=status.msg)

@bp.route('/<results_id>/')
def retrieve_results(results_id):
    # get search results
    results_status_found = flask.g.db.find(
        tesserae.db.entities.ResultsStatus.collection,
        results_id=results_id
    )
    if not results_status_found:
        response = flask.Response('Could not find results_id')
        response.status_code = 404
        return response

    match_set_found = flask.g.db.find(
        tesserae.db.entities.MatchSet.collection,
        _id=ObjectId(results_status_found[0].match_set_id)
    )
    if not match_set_found:
        response = flask.Response('Could not find MatchSet')
        response.status_code = 404
        return response
    params = match_set_found[0].parameters

    # matches = flask.g.db.get_search_matches(match_set_found[0].id)
    matches = flask.g.db.find(tesserae.db.entities.Match.collection,
            match_set=ObjectId(match_set_found[0].id))
    matches = [m.json_encode() for m in matches]
    response = flask.Response(
        response=gzip.compress(flask.json.dumps({
            'data': params,
            'parallels': matches
        }).encode()),
        mimetype='application/json',
    )
    response.status_code = 200
    response.status = '200 OK'
    response.headers['Content-Encoding'] = 'gzip'
    return response
