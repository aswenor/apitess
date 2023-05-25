"""The family of /parallels/ endpoints"""
import gzip
import os
import queue
import uuid

import flask
import tesserae.db.entities
import tesserae.utils.exports
import tesserae.utils.search
from bson.objectid import ObjectId
from flask_cors import cross_origin
from tesserae.matchers.text_options import TextOptions
from tesserae.utils.downloads import ResultsWriter

import apitess.errors
from apitess.utils import common_retrieve_status, get_page_options_or_error

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


@bp.route('/', methods=(
    'POST',
    'OPTIONS',
))
@cross_origin(expose_headers='Location')
def submit_search():
    """Run a Tesserae search"""
    error_response, received = apitess.errors.check_body(flask.request)
    if error_response:
        return error_response
    requireds = {'source', 'target', 'method'}
    miss_error = apitess.errors.check_requireds(received, requireds)
    if miss_error:
        return miss_error

    source = received['source']
    target = received['target']

    errors = _validate_units(source, 'source')
    errors.extend(_validate_units(target, 'target'))
    if errors:
        return apitess.errors.error(
            400,
            data=received,
            message=('The following errors were found in source and target '
                     'unit specifications:\n{}'.format('\n\t'.join(errors))))

    source_object_id = source['object_id']
    target_object_id = target['object_id']
    results = flask.g.db.find(
        tesserae.db.entities.Text.collection,
        _id=[ObjectId(source_object_id),
             ObjectId(target_object_id)])
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
            message=('Unable to find the following object_id(s) among the '
                     'texts in the database:\n\t{}'.format(
                         '\n\t'.join(errors))))
    source_text = results[source_object_id]
    target_text = results[target_object_id]

    method_requireds = {
        'original': {
            'name', 'feature', 'stopwords', 'score_basis', 'freq_basis',
            'max_distance', 'distance_basis'
        },
        'greek_to_latin': {
            'name', 'greek_stopwords', 'latin_stopwords', 'freq_basis',
            'max_distance', 'distance_basis'
        },
    }
    method = received['method']
    if 'name' not in method:
        return apitess.errors.error(400,
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
            message=('The specified method is missing the following required '
                     'key(s): {}'.format(', '.join(missing))))

    if 'min_score' in method:
        try:
            method['min_score'] = float(method['min_score'])
        except ValueError:
            return apitess.error.error(
                400,
                data=received,
                message=(f'Specified minimum score ({method["min_score"]}) '
                         'could not be converted into a number'))
    else:
        method['min_score'] = 0

    results_id = tesserae.utils.search.check_cache(flask.g.db, source, target,
                                                   method)
    if results_id:
        response = flask.Response()
        response.status_code = 303
        response.status = '303 See Other'
        # Redirect should point to paginated results
        response.headers['Location'] = os.path.join(
            flask.request.base_url, results_id, '?' + '&'.join(
                f'{a}={b}' for a, b in {
                    'sort_by': 'score',
                    'sort_order': 'descending',
                    'per_page': '100',
                    'page_number': '0'
                }.items()))
        return response

    response = flask.Response()
    response.status_code = 201
    response.status = '201 Created'
    results_id = uuid.uuid4().hex
    # we want the final '/' on the URL
    response.headers['Location'] = os.path.join(flask.request.base_url,
                                                results_id, '')

    try:
        search_params = {
            'source': TextOptions(source_text, source['units'], sourceDivision),
            'target': TextOptions(target_text, target['units'], targetDivision),
        }
        search_params.update(
            {key: method[key]
             for key in method if key != 'name'})
        tesserae.utils.search.submit_search(flask.g.jobqueue, flask.g.db,
                                            results_id, method['name'],
                                            search_params)
    except queue.Full:
        return apitess.error.error(
            500,
            data=received,
            message=('The search request could not be added to the queue. '
                     'Please try again in a few minutes'))
    return response


@bp.route('/<results_id>/status/')
@cross_origin()
def retrieve_status(results_id):
    return common_retrieve_status(flask.g.db.find, results_id,
                                  tesserae.utils.search.NORMAL_SEARCH)


@bp.route('/<results_id>/downloads/')
@cross_origin()
def download(results_id):
    results_status_found = flask.g.db.find(
        tesserae.db.entities.Search.collection,
        results_id=results_id,
        search_type=tesserae.utils.search.NORMAL_SEARCH)
    if not results_status_found:
        response = flask.Response('Could not find results_id')
        response.status_code = 404
        return response
    status = results_status_found[0]
    status.update_last_queried()
    flask.g.db.update(status)
    if status.status == tesserae.db.entities.Search.DONE:
        return flask.send_file(tesserae.utils.downloads.get_results_filename(
            status, ResultsWriter.RESULTS_DIR),
                               as_attachment=True)
    response = flask.Response('Download for these results are not yet ready')
    response.status_code = 404
    return response


@bp.route('/<results_id>/')
@cross_origin()
def retrieve_results(results_id):
    # get search results
    results_status_found = flask.g.db.find(
        tesserae.db.entities.Search.collection,
        results_id=results_id,
        search_type=tesserae.utils.search.NORMAL_SEARCH)
    if not results_status_found:
        response = flask.Response('Could not find results_id')
        response.status_code = 404
        return response
    if results_status_found[0].status != tesserae.db.entities.Search.DONE:
        status_url = os.path.join(flask.request.base_url, results_id, 'status',
                                  '')
        response = flask.Response(
            f'Unable to retrieve results; check {status_url} endpoint.')
        response.headers['Cache-Control'] = 'no-store'
        response.status_code = 404
        return response

    url_query_params = flask.request.args
    page_options, err = get_page_options_or_error(url_query_params)
    if err:
        return err

    search_id = results_status_found[0].id
    response = flask.Response(
        response=gzip.compress(
            flask.json.dumps({
                'data':
                results_status_found[0].parameters,
                'max_score':
                tesserae.utils.search.get_max_score(flask.g.db, search_id),
                'total_count':
                tesserae.utils.search.get_results_count(flask.g.db, search_id),
                'parallels':
                tesserae.utils.search.get_results(flask.g.db, search_id,
                                                  page_options)
            }).encode('utf-8')),
        mimetype='application/json',
    )
    response.status_code = 200
    response.status = '200 OK'
    response.headers['Content-Encoding'] = 'gzip'

    results_status_found[0].update_last_queried()
    flask.g.db.update(results_status_found[0])
    return response
