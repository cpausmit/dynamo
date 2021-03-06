import re
import collections
import logging

from dynamo.registry.registry import RegistryDatabase
from dynamo.dataformat import Configuration, Block, ObjectError, ConfigurationError

LOG = logging.getLogger(__name__)

class UnhandledCopyExists(object):
    """
    Check for pending transfer requests made to Dealer.
    Sets one attr:
      unhandled_copy_exists_to
    """

    produces = ['unhandled_copy_exists']

    def __init__(self, config = None):
        config = Configuration(config)

        self.registry = RegistryDatabase(config.get('registry', None))

    def load(self, inventory):
        # collect the name of items that are not yet activated or are activated but not queued
        sql = 'SELECT i.`item` FROM `copy_request_items` AS i INNER JOIN `copy_requests` AS r ON r.`id` = i.`request_id`'
        sql += ' WHERE r.`status` = \'new\''
        items = self.registry.db.query(sql)
        items += self.registry.db.query('SELECT `item` FROM `active_copies` WHERE `status` = \'new\'')

        for item_name in items:
            try:
                dataset_name, block_name = Block.from_full_name(item_name)
            except ObjectError:
                dataset_name, block_name = item_name, None

            try:
                dataset = inventory.datasets[dataset_name]
            except KeyError:
                continue

            if block_name is not None:
                block = dataset.find_block(block_name)
                if block is None:
                    continue

            dataset.attr['unhandled_copy_exists'] = True
