"""policy.py

Requirements: $PATH must include pomdpsol-appl for 'appl' policies and
pomdpsol-aitoolbox for 'aitoolbox' policies.

"""
from __future__ import division
import collections
import os
import time
import copy
import random
import math
import subprocess
import numpy as np
from .pomdp import POMDPPolicy, POMDPModel
from . import util
from .util import ensure_dir, equation_safe_filename
from . import work_learn_problem as wlp
from . import param

ZMDP_ALIAS = os.environ.get('ZMDP_ALIAS', 'pomdpsol-zmdp')

class Policy:
    """Policy class

    Assumes policy files for appl policies live in relative folder 'policies'

    """
    def __init__(self, policy_type, n_worker_classes, params_gt,
                 **kwargs):
        print 'Reinitializing policy'
        default_discount = 0.99
        self.policy = policy_type
        self.epsilon = kwargs.get('epsilon', None)
        self.explore_actions = kwargs.get('explore_actions', None)
        self.explore_policy = kwargs.get('explore_policy', None)
        self.thompson = bool(kwargs.get('thompson', False))
        self.hyperparams = kwargs.get('hyperparams', None)
        self.desired_accuracy = params_gt.get('desired_accuracy', None)

        if self.rl_p():
            name = kwargs['hyperparams']
            cls = getattr(param, name)
            self.model = POMDPModel(
                n_worker_classes, params=params_gt,
                hyperparams=cls(params_gt, n_worker_classes),
                estimate_all=True)
            if self.explore_policy is not None:
                self.explore_policy = Policy(
                    policy_type=self.explore_policy['type'],
                    n_worker_classes=n_worker_classes,
                    params_gt=params_gt,
                    **self.explore_policy)
        else:
            self.model = POMDPModel(n_worker_classes, params=params_gt)

        if self.policy in ('appl', 'zmdp'):
            self.discount = kwargs.get('discount', default_discount)
            self.timeout = kwargs.get('timeout', None)
        elif self.policy == 'aitoolbox':
            self.discount = kwargs.get('discount', default_discount)
            self.horizon = kwargs['horizon']
        elif self.policy == 'test_and_boot':
            self.teach_type = kwargs.get('teach_type', None)
            self.n_teach = kwargs.get('n_teach', 0)
            self.n_blocks = kwargs.get('n_blocks', None)
            if self.n_blocks != 0:
                self.n_test = kwargs['n_test']
                self.n_work = kwargs['n_work']
                self.accuracy = kwargs['accuracy']
                n_test_actions = len(
                    [a for a in self.model.actions if a.is_quiz()])
                self.accuracy_window = kwargs.get('accuracy_window', None)
                if self.accuracy_window is None:
                    self.accuracy_window = self.n_test * n_test_actions
            self.final_action = kwargs.get('final_action', 'work')
        elif self.policy != 'work_only':
            raise NotImplementedError

        self.params_estimated = dict()
        self.hparams_estimated = dict()
        self.estimate_times = dict()
        self.resolve_times = dict()
        self.external_policy = None
        self.use_explore_policy = False

    def rl_p(self):
        """Policy does reinforcement learning."""
        return self.epsilon is not None or self.thompson

    def get_epsilon_probability(self, worker, t, budget_frac):
        """Return probability specified by the given exploration function.

        Exploration function is a function of the worker (w or worker)
        the current timestep (t), and the fraction of the exploration
        budget (f or budget_frac).

        WARNING: Evaluates the expression in self.epsilon without security
        checks.

        """
        # Put some useful variable abbreviations in the namespace.
        w = worker
        f = budget_frac
        e = math.e
        if isinstance(self.epsilon, basestring):
            return eval(self.epsilon)
        else:
            return self.epsilon

    def set_use_explore_policy(self, worker_n, budget_spent=None, budget_explore=None, t=None, reserved=False):
        """Set whether the policy should explore."""
        if budget_spent is None or budget_explore is None:
            budget_explore_frac = None
        elif budget_explore == 0:
            budget_explore_frac = 1
        else:
            budget_explore_frac = budget_spent / budget_explore
        self.use_explore_policy = (
            not reserved and
            self.epsilon is not None and
            self.explore_policy is not None and
            np.random.random() <= self.get_epsilon_probability(
                worker_n, t, budget_explore_frac))

    def prep_worker(self, model_filepath, policy_filepath, history,
                    budget_spent, budget_explore, reserved,
                    resolve_random_restarts=1,
                    resolve_min_worker_interval=10, resolve_max_n=10,
                    previous_workers=None):
        """Reestimate and resolve as needed.

        Don't resolve more frequently than resolve_min_worker_interval.

        Args:
            history (.history.History): History of workers.
                IMPORTANT: Do not call .history.History.new_worker() before
                running this function or the worker count will be incorrect.
            resolve_random_restarts (int): Number of random restarts to use
                when re-estimating model.
            previous_workers (Optional[int]): Number of previous workers.
                Defaults to one less than number of workers in history object.

        """
        worker = history.n_workers()
        if previous_workers is None:
            previous_workers = worker
        t = 0
        self.set_use_explore_policy(
            worker_n=previous_workers,
            budget_spent=budget_spent,
            budget_explore=budget_explore,
            t=t,
            reserved=reserved)

        resolve_p = (self.policy in ('appl', 'zmdp', 'aitoolbox') and
                     (self.external_policy is None or
                      (self.rl_p() and not self.use_explore_policy)))
        if self.resolve_times:
            if resolve_min_worker_interval is not None and worker - max(self.resolve_times) < resolve_min_worker_interval:
                resolve_p = False
            if resolve_max_n is not None and len(self.resolve_times) >= resolve_max_n:
                resolve_p = False

        estimate_p = self.rl_p() and resolve_p
        model = self.model
        if estimate_p:
            start = time.clock()
            model.estimate(history=history,
                           last_params=(len(self.params_estimated) > 0),
                           random_restarts=resolve_random_restarts)
            if self.thompson:
                model.thompson_sample()
            self.estimate_times[worker] = time.clock() - start
            self.params_estimated[worker] = copy.deepcopy(
                model.get_params_est())
            self.hparams_estimated[worker] = copy.deepcopy(model.hparams)
        if resolve_p:
            utime1, stime1, cutime1, cstime1, _ = os.times()
            self.external_policy = self.run_solver(
                model_filepath=model_filepath, policy_filepath=policy_filepath)
            utime2, stime2, cutime2, cstime2, _ = os.times()
            # All solvers are run as subprocesses, so count elapsed
            # child process time.
            self.resolve_times[worker] = cutime2 - cutime1 + \
                                         cstime2 - cstime1

    def get_next_action(self, history,
                        budget_spent, budget_explore, belief=None,
                        previous_workers=None):
        """Return next action and whether or policy is exploring.

        Args:
            previous_workers (Optional[int]): Number of previous workers.
                Defaults to one less than number of workers in history object.

        """
        valid_actions = self.get_valid_actions(history)
        worker = history.n_workers() - 1
        t = history.n_t(worker)
        if previous_workers is None:
            previous_workers = worker
        budget_explore_frac = budget_spent / budget_explore
        if (self.epsilon is not None and
                self.explore_policy is None and
                np.random.random() <= self.get_epsilon_probability(
                    previous_workers, t, budget_explore_frac)):
            valid_explore_actions = [
                i for i in valid_actions if
                self.model.actions[i].get_type() in self.explore_actions]
            return np.random.choice(valid_explore_actions), True
        elif self.use_explore_policy:
            next_a, _ = self.explore_policy.get_next_action(
                history, budget_spent, budget_explore, belief)
            return next_a, True
        else:
            return self.get_best_action(history, belief), False


    def get_best_action(self, history, belief=None):
        """Get best action according to policy.

        If policy requires an external_policy, assumes it already exists.

        self.n_blocks should be None unless teaching actions disabled.

        Accuracy for test_and_boot policy is averaged across question
        types.

        Args:
            history (History object):   Defined in history.py.

        Returns: Action index.

        """
        valid_actions = self.get_valid_actions(history)
        model = self.model
        a_ask = model.actions.index(wlp.Action('ask'))
        a_boot = model.actions.index(wlp.Action('boot'))
        worker = history.n_workers() - 1
        current_AO = history.history[-1]
        if len(current_AO) == 0:
            current_actions = []
            current_observations = []
        else:
            current_actions, current_observations, _ = zip(*current_AO)
        n_actions = len(current_actions)
        if self.policy == 'work_only':
            return a_ask
        elif self.policy == 'test_and_boot':
            if self.teach_type is not None:
                # Make sure to teach each skill at least n times.
                # Select skills in random order, but teach each skill as a batch.
                if self.teach_type == 'exp':
                    teach_actions = [i for i, a in enumerate(model.actions) if
                                     a.is_quiz()]
                    teach_counts = collections.defaultdict(int)
                    for i in xrange(len(current_actions) - 1):
                        a1 = model.actions[current_actions[i]]
                        a2 = model.actions[current_actions[i + 1]]
                        if a1.is_quiz() and a2.name == 'exp':
                            teach_counts[current_actions[i]] += 1
                elif self.teach_type == 'tell':
                    teach_actions = [i for i, a in enumerate(model.actions) if
                                     a.name == 'tell']
                    teach_counts = collections.Counter(
                        [a for a in current_actions if a in teach_actions])
                teach_actions_remaining = [a for a in teach_actions if
                                           teach_counts[a] < self.n_teach]
                teach_actions_in_progress = [a for a in teach_actions_remaining if
                                             teach_counts[a] > 0]
                if n_actions == 0:
                    if teach_actions_remaining:
                        return random.choice(teach_actions_remaining)
                    else:
                        return a_ask
                else:
                    last_action = current_actions[-1]
                    if (self.teach_type == 'exp' and
                            last_action in teach_actions_remaining):
                        return model.actions.index(wlp.Action('exp'))
                    elif len(teach_actions_in_progress) > 0:
                        return random.choice(teach_actions_in_progress)
                    elif len(teach_actions_remaining) > 0:
                        return random.choice(teach_actions_remaining)
            # Test & work  phase.
            if self.final_action == 'work':
                a_final = a_ask
            elif self.final_action == 'boot':
                a_final = a_boot
            else:
                raise Exception('Unexpected final action type')
            n_work_actions = len([a for a in current_actions if
                                  a == a_ask])
            # If all blocks done, take final action.
            test_actions = [i for i, a in enumerate(model.actions) if
                            a.is_quiz()]
            if self.n_blocks is not None:
                if self.n_blocks == 0:
                    return a_final
                block_length = len(test_actions) * self.n_test + self.n_work
                n_blocks_completed = len(current_actions) / block_length
                if n_blocks_completed >= self.n_blocks:
                    return a_final
            last_action_block = util.last_true(
                current_actions, lambda a: model.actions[a].is_quiz())
            test_counts = collections.Counter(last_action_block)
            if self.n_work == 0 or n_work_actions % self.n_work == 0:
                test_actions_remaining = [a for a in test_actions if
                                          test_counts[a] < self.n_test]
                if len(test_actions_remaining) == 0:
                    # Testing done. Check accuracy.
                    test_answers = current_observations[
                        -1 * self.accuracy_window:]
                    assert not any(model.observations[i] in ['term', 'null'] for
                        i in test_answers)
                    concat_answers = ''.join(model.observations[i] for
                                             i in test_answers)
                    accuracy = sum(v == 'r' for v in concat_answers) / len(concat_answers)
                    if accuracy >= self.accuracy:
                        return a_ask
                    else:
                        return a_boot
                else:
                    return random.choice(test_actions_remaining)
            else:
                return a_ask
        elif self.policy in ('appl', 'aitoolbox', 'zmdp'):
            rewards = self.external_policy.get_action_rewards(belief)
            valid_actions_with_rewards = set(valid_actions).intersection(
                set(rewards))
            if len(valid_actions_with_rewards) == 0:
                raise Exception('No valid actions in policy')
            max_reward = max(rewards.itervalues())
            valid_rewards = dict((a, rewards[a]) for a in valid_actions_with_rewards)
            max_valid_reward = max(valid_rewards.itervalues())
            if max_reward > max_valid_reward:
                print 'Warning: best reward not available'
            # Take random best action.
            best_valid_action = random.choice(
                [a for a in valid_rewards if
                 valid_rewards[a] == max_valid_reward])
            return best_valid_action
        else:
            raise NotImplementedError

    def run_solver(self, model_filepath, policy_filepath):
        """Run POMDP solver.
        
        Args:
            model_filepath (str):       Path for input to POMDP solver.
            policy_filepath (str):      Path for computed policy.

        Returns:
            policy (POMDPPolicy)

        """
        model = self.model
        if self.policy == 'appl':
            with open(model_filepath, 'w') as f:
                model.write_pomdp(f, discount=self.discount)
            args = ['pomdpsol-appl',
                    model_filepath,
                    '-o', policy_filepath]
            if self.timeout is not None:
                args += ['--timeout', str(self.timeout)]
            _ = subprocess.check_output(args)
            return POMDPPolicy(policy_filepath,
                               file_format='policyx')
        elif self.policy == 'aitoolbox':
            with open(model_filepath, 'w') as f:
                model.write_txt(f)
            args = ['pomdpsol-aitoolbox',
                    '--input', model_filepath,
                    '--output', policy_filepath,
                    '--discount', str(self.discount),
                    '--horizon', str(self.horizon),
                    '--n_states', str(len(model.states)),
                    '--n_actions', str(len(model.actions)),
                    '--n_observations', str(len(model.observations))]
            _ = subprocess.check_output(args)
            return POMDPPolicy(policy_filepath,
                               file_format='aitoolbox',
                               n_states=len(model.states))
        elif self.policy == 'zmdp':
            with open(model_filepath, 'w') as f:
                model.write_pomdp(f, discount=self.discount)
            args = [ZMDP_ALIAS,
                    'solve', model_filepath,
                    '-o', policy_filepath]
            if self.timeout is not None:
                args += ['-t', str(self.timeout)]
            _ = subprocess.check_output(args)
            return POMDPPolicy(policy_filepath,
                               file_format='zmdp',
                               n_states=len(model.states))


    def get_valid_actions(self, history):
        """Return valid action indices based on the history."""
        current_AO = history.history[-1]
        if len(current_AO) == 0:
            current_actions = []
            current_observations = []
        else:
            current_actions, current_observations, _ = zip(*current_AO)

        try:
            last_action = self.model.actions[current_actions[-1]]
        except IndexError:
            last_action = None
        return [i for i, a in enumerate(self.model.actions) if
                a.valid_after(last_action)]

    def __str__(self):
        if self.policy in ('appl', 'zmdp'):
            s = self.policy + '-d{:.3f}'.format(self.discount)
            if self.timeout is not None:
                s += '-tl{}'.format(self.timeout)
        elif self.policy == 'aitoolbox':
            s = 'ait' + '-d{:.3f}'.format(self.discount)
            s += '-h{}'.format(self.horizon)
        elif self.policy == 'test_and_boot':
            s = self.policy
            if self.n_teach > 0:
                s += '-n_teach_{}_{}'.format(self.teach_type, self.n_teach)
            if self.n_blocks != 0:
                s += '-n_test_{}-n_work_{}-acc_{}_last_{}'.format(
                    self.n_test, self.n_work,
                    self.accuracy, self.accuracy_window)
            if self.n_blocks is not None:
                s += '-n_blocks_{}-final_{}'.format(
                    self.n_blocks, self.final_action)
        elif self.policy == 'work_only':
            s = self.policy
        else:
            raise NotImplementedError

        if self.rl_p():
            if self.epsilon is not None:
                s += '-eps_{}'.format(equation_safe_filename(self.epsilon))
                if self.explore_policy is not None:
                    s += '-explore_p_{}'.format(self.explore_policy)
                else:
                    s += '-explore_{}'.format('_'.join(self.explore_actions))
            if self.thompson:
                s += '-thomp'
            if self.hyperparams and self.hyperparams != 'HyperParams':
                s += '-{}'.format(self.hyperparams)
            s += '-cl{}'.format(self.model.n_worker_classes)
            if self.desired_accuracy is not None:
                s += '-acc{:.2f}'.format(self.desired_accuracy)
        return s
