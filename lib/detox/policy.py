class DeletionPolicy(object):
    """
    Base class for deletion policies.
    """

    DEC_KEEP, DEC_DELETE, DEC_KEEP_OVERRIDE = range(3)

    def __init__(self, name, fct, decision):
        """
        Initialize with a function(dataset_replica, dataset_demand)->bool
        eval() returns decision if fct evaluates to True.
        Otherwise DEC_KEEP is returned.
        """

        self.name = name
        self.decision = decision
        self._fct = fct

    def eval(self, replica, dataset_demand):
        if self._fct(replica, dataset_demand):
            return self.decision
        else:
            return DeletionPolicy.DEC_KEEP


class DeletionPolicyManager(object):
    """
    Holds a stack of deletion policies and make a collective decision on a replica.
    """

    def __init__(self, policies):
        self._policies = policies

    def add_policy(self, policy):
        if type(policy) is list:
            self._policies += policy
        else:
            self._policies.append(policy)

    def decision(self, replica, demand):
        """
        Loop over the policies. Return DELETE if at least one policy hits, unless
        there is a KEEP_OVERRIDE.
        """
        
        result = DeletionPolicy.DEC_KEEP

        for policy in self._policies:
            dec = policy.decision(replica, demand)
            if dec == DeletionPolicy.DEC_DELETE:
                result = DeletionPolicy.DEC_DELETE

            elif dec == DeletionPolicy.DEC_KEEP_OVERRIDE:
                return DeletionPolicy.DEC_KEEP

        # TODO Add function to record policy hits

        return result