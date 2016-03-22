import json
import logging

from common.interface.datasetinfo import DatasetInfoSourceInterface
from common.interface.webservice import RESTService
from common.dataformat import Dataset, Block
from common.misc import unicode2str
import common.configuration as config

logger = logging.getLogger(__name__)

class DBSInterface(DatasetInfoSourceInterface):
    """
    Interface to DBS using DBSReader REST API.
    """
    
    def __init__(self):
        self._interface = RESTService(config.dbs.url_base)

    def get_dataset(self, name): # override
        ds_records = self._make_request('datasets', ['dataset=' + name, 'detail=True'])
        if len(ds_records) == 0:
            logger.warning('Dataset %s not found on record.', name)
            return dataset

        block_records = self._make_request('blocksummaries', ['dataset=' + name, 'detail=True'])

        dataset = self._construct_dataset(ds_records[0], block_records)

        return dataset

    def get_datasets(self, names): # override
        datasets = []

        first = 0
        while first < len(names):
            # fetch data 1000 at a time
            last = first + 1000
            ds_records = self._make_request('datasetlist', {'dataset': ['%s' % name for name in names[first:last]], 'detail': True}, method = 'POST', format = 'json')            

            # This is still way too slow - have to make one API call (O(1)s) for each dataset.
            # We are actually only interested in the number of blocks in the dataset; DBS datasetlist does not give you that.
            for ds_record in ds_records:
                block_records = self._make_request('blocksummaries', ['dataset=' + ds_record['dataset']] + ['detail=True'])
            
                dataset = self._construct_dataset(ds_record, block_records)
                datasets.append(dataset)

            first = last
        
        return datasets

    def _construct_dataset(self, ds_record, block_records):
        ds_name = ds_record['dataset']
        dataset = Dataset(ds_name)
        dataset.is_valid = (ds_record['dataset_access_type'] == 'VALID')

        for block_record in block_records:
            if block_record['dataset'] != ds_name:
                continue

            block_name = block_record['block_name'].replace(dataset.name + '#', '')

            if block_record['open_for_writing'] == 1:
                is_open = True
                dataset.is_open = True
            else:
                is_open = False

            block = Block(block_name, dataset = dataset, size = block_record['block_size'], num_files = block_record['file_count'], is_open = is_open)
        
            dataset.blocks.append(block)

        dataset.size = sum([b.size for b in dataset.blocks])
        dataset.num_files = sum([b.num_files for b in dataset.blocks])

        return dataset

    def _make_request(self, resource, options = [], method = 'GET', format = 'url'):
        """
        Make a single DBS request call. Returns a list of dictionaries.
        """

        resp = self._interface.make_request(resource, options = options, method = method, format = format)
        logger.info('DBS returned a response of ' + str(len(resp)) + ' bytes.')

        result = json.loads(resp)
        logger.debug(result)

        unicode2str(result)

        return result


if __name__ == '__main__':

    from argparse import ArgumentParser

    parser = ArgumentParser(description = 'DBS Interface')

    parser.add_argument('command', metavar = 'COMMAND', help = 'Command to execute.')
    parser.add_argument('options', metavar = 'EXPR', nargs = '+', default = [], help = 'Option string as passed to PhEDEx datasvc.')

    args = parser.parse_args()
    
    logger.setLevel(logging.DEBUG)
    
    command = args.command

    interface = DBSInterface()

    if command == 'datasetlist':
        # options: dataset=/A1/B1/C1,/A2/B2/C2,...
        key, eq, values = args.options[0].partition('=')
        datasets = values.split(',')
        options = {'dataset': datasets}
        if 'detail=True' in args.options:
            options['detail'] = True

        print interface._make_request(command, options, method = 'POST', format = 'json')

    else:
        print interface._make_request(command, args.options)