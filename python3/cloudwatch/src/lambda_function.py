import base64
import gzip
import json
import logging
import os
from io import BytesIO

from python3.shipper.shipper import LogzioShipper

KEY_INDEX = 0
VALUE_INDEX = 1
LOG_LEVELS = ['ALERT', 'TRACE', 'DEBUG', 'NOTICE', 'INFO', 'WARN',
              'WARNING', 'ERROR', 'ERR', 'CRITICAL', 'CRIT',
              'FATAL', 'SEVERE', 'EMERG', 'EMERGENCY']

LOG_GROUP_TO_PREFIX = {
    "/aws/apigateway/": "aws/apigateway",
    "/aws/rds/cluster/": "aws/rds",
    "/aws/cloudhsm/": "aws/cloudhsm",
    "aws-cloudtrail-logs-": "aws/cloudtrail",
    "/aws/codebuild/": "aws/codebuild",
    "/aws/connect/": "aws/connect",
    "/aws/elasticbeanstalk/": "aws/elasticbeanstalk",
    "/aws/ecs/": "aws/ecs",
    "/aws/eks/": "aws/eks",
    "/aws-glue/": "glue",
    "AWSIotLogsV2": "aws/iot",
    "/aws/lambda/": "aws/lambda",
    "/aws/macie/": "aws/macie",
    "/aws/amazonmq/broker/": "aws/amazonmq"
}

PYTHON_EVENT_SIZE = 3
NODEJS_EVENT_SIZE = 4
LAMBDA_LOG_GROUP = '/aws/lambda/'


# set logger
logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _extract_aws_logs_data(event):
    # type: (dict) -> dict
    event_str = event['awslogs']['data']
    try:
        logs_data_decoded = base64.b64decode(event_str)
        logs_data_unzipped = gzip.GzipFile(fileobj=BytesIO(logs_data_decoded))
        logs_data_unzipped = logs_data_unzipped.read()
        logs_data_dict = json.loads(logs_data_unzipped)
        return logs_data_dict
    except ValueError as e:
        logger.error("Got exception while loading json, message: {}".format(e))
        raise ValueError("Exception: json loads")


def _extract_lambda_log_message(log):
    # type: (dict) -> None
    str_message = str(log['message'])
    try:
        start_level = str_message.index('[')
        end_level = str_message.index(']')
        log_level = str_message[start_level + 1:end_level].upper()
        if log_level in LOG_LEVELS:
            log['log_level'] = log_level
            start_split = end_level + 2
        else:
            start_split = 0
    except ValueError:
        # Let's try without log level
        start_split = 0
    message_parts = str_message[start_split:].split('\t')
    size = len(message_parts)
    if size == PYTHON_EVENT_SIZE or size == NODEJS_EVENT_SIZE:
        log['@timestamp'] = message_parts[0]
        log['requestID'] = message_parts[1]
        log['message'] = message_parts[size - 1]
    if size == NODEJS_EVENT_SIZE:
        log['log_level'] = message_parts[2]


def _add_timestamp(log):
    # type: (dict) -> None
    if '@timestamp' not in log:
        log['@timestamp'] = str(log['timestamp'])
        del log['timestamp']


def _parse_to_json(log):
    # type: (dict) -> None
    try:
        if os.environ['FORMAT'].lower() == 'json':
            json_object = json.loads(log['message'])
            if isinstance(json_object, list):
                # In this case, json_object doesn't have the items() method
                logger.info('Field message is a list and cannot be parsed to JSON')
                return
            for key, value in json_object.items():
                log[key] = value
    except Exception as e:
        logger.warning(f'Error occurred while trying to parse log to JSON: {e}. Field will be passed as string.')
        pass


def _parse_cloudwatch_log(log, additional_data):
    # type: (dict, dict) -> bool
    _add_timestamp(log)
    if LAMBDA_LOG_GROUP in additional_data['logGroup']:
        _extract_lambda_log_message(log)
    log.update(additional_data)
    _parse_to_json(log)
    return True


def _get_additional_logs_data(aws_logs_data, context):
    # type: (dict, 'LambdaContext') -> dict
    additional_fields = ['logGroup', 'logStream', 'messageType', 'owner']
    additional_data = dict(
        (key, aws_logs_data[key]) for key in additional_fields)
    try:
        if 'logGroup' in additional_data:
            namespace = get_service_by_log_group_prefix(additional_data['logGroup'])
            if namespace == '':
                logger.info(f'Mapping from log group to namespace does not exist for log group {additional_data["logGroup"]}')
            else:
                additional_data['namespace'] = namespace
        else:
            logger.info('Field logGroup does not appear in data. Field namespace will not be added')
    except Exception as e:
        logger.warning(f'Error while trying to get namespace: {e}')
    try:
        additional_data['function_version'] = context.function_version
        additional_data['invoked_function_arn'] = context.invoked_function_arn
    except KeyError:
        logger.info(
            'Failed to find context value. Continue without adding it to the log')

    try:
        # If ENRICH has value, add the properties
        if os.environ['ENRICH']:
            properties_to_enrich = os.environ['ENRICH'].split(";")
            for property_to_enrich in properties_to_enrich:
                property_key_value = property_to_enrich.split("=")
                additional_data[property_key_value[KEY_INDEX]
                                ] = property_key_value[VALUE_INDEX]
    except KeyError:
        pass

    try:
        additional_data['type'] = os.environ['TYPE']
    except KeyError:
        logger.info("Using default TYPE 'logzio_cloudwatch_lambda'.")
        additional_data['type'] = 'logzio_cloudwatch_lambda'
    return additional_data


def lambda_handler(event, context):
    # type (dict, 'LambdaContext') -> None

    aws_logs_data = _extract_aws_logs_data(event)
    additional_data = _get_additional_logs_data(aws_logs_data, context)
    shipper = LogzioShipper()

    logger.info("About to send {} logs".format(
        len(aws_logs_data['logEvents'])))
    for log in aws_logs_data['logEvents']:
        if not isinstance(log, dict):
            raise TypeError(
                "Expected log inside logEvents to be a dict but found another type")
        if _parse_cloudwatch_log(log, additional_data):
            shipper.add(log)

    shipper.flush()


def get_service_by_log_group_prefix(log_group):
    for key in LOG_GROUP_TO_PREFIX:
        if log_group.startswith(key):
            return LOG_GROUP_TO_PREFIX[key]
    return ''
