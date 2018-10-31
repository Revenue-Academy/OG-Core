import json
import os
import collections as collect
import six
import re
import numpy as np
import pickle
import scipy.interpolate as si

# import ogusa
from ogusa.parametersbase import ParametersBase
from ogusa import elliptical_u_est
from ogusa import demographics
from ogusa import income
from ogusa import txfunc
from ogusa.utils import REFORM_DIR, BASELINE_DIR, TC_LAST_YEAR
# from ogusa import elliptical_u_est


class Specifications(ParametersBase):
    """
    Inherits ParametersBase. Implements the PolicyBrain API for OG-USA
    """
    DEFAULTS_FILENAME = 'default_parameters.json'

    def __init__(self,
                 run_micro=False, output_base=BASELINE_DIR,
                 baseline_dir=BASELINE_DIR, test=False, time_path=True,
                 baseline=False, constant_rates=True,
                 tax_func_type='DEP', analytical_mtrs=False,
                 age_specific=False, reform={}, guid='', data='cps',
                 flag_graphs=False, client=None, num_workers=1):
        super(Specifications, self).__init__()

        # reads in default parameter values
        self._vals = self._params_dict_from_json_file()

        self.test = test
        self.time_path = time_path
        self.output_base = output_base
        self.baseline_dir = baseline_dir
        self.baseline = baseline
        self.reform = reform
        self.guid = guid
        self.data = data
        self.flag_graphs = flag_graphs
        self.num_workers = num_workers

        # does cheap calculations to find parameter values
        self.initialize()

        # put anything in kwargs that is also in json file below
        # initialize()
        self.constant_rates = constant_rates
        self.tax_func_type = tax_func_type
        self.analytical_mtrs = analytical_mtrs
        self.age_specific = age_specific

        # does more costly tax function estimation
        if run_micro:
            self.get_tax_function_parameters(self, client, run_micro=True)

        self.parameter_warnings = ''
        self.parameter_errors = ''
        self._ignore_errors = False

    def initialize(self):
        """
        ParametersBase reads JSON file and sets attributes to self
        Next call self.compute_default_params for further initialization
        Parameters:
        -----------
        run_micro: boolean that indicates whether to estimate tax funtions
                   from microsim model
        """
        for name, data in self._vals.items():
            intg_val = data.get('integer_value', None)
            bool_val = data.get('boolean_value', None)
            string_val = data.get('string_value', None)
            values = data.get('value', None)
            setattr(self, name, self._expand_array(values, intg_val,
                                                   bool_val, string_val))
        if self.test:
            # Make smaller statespace for testing
            self.S = int(40)
            self.lambdas = np.array([0.6, 0.4]).reshape(2, 1)
            self.J = self.lambdas.shape[0]
            self.maxiter = 35
            self.mindist_SS = 1e-6
            self.mindist_TPI = 1e-3
            self.nu = .4

        self.compute_default_params()

    def compute_default_params(self):
        """
        Does cheap calculations to return parameter values
        """
        # get parameters of elliptical utility function
        self.b_ellipse, self.upsilon = elliptical_u_est.estimation(
            self.frisch,
            self.ltilde
        )
        # determine length of budget window from start year and last
        # year in TC
        self.BW = int(TC_LAST_YEAR - self.start_year + 1)
        # Find number of economically active periods of life
        self.E = int(self.starting_age * (self.S / (self.ending_age -
                                                    self.starting_age)))
        # Find rates in model periods from annualized rates
        self.beta = (self.beta_annual ** ((self.ending_age -
                                          self.starting_age) / self.S))
        self.delta = (1 - ((1 - self.delta_annual) **
                           ((self.ending_age - self.starting_age) / self.S)))
        self.g_y = ((1 + self.g_y_annual) ** ((self.ending_age -
                                               self.starting_age) /
                                              self.S) - 1)
        self.delta_tau = (1 - ((1 - self.delta_tau_annual) **
                               ((self.ending_age - self.starting_age) /
                                self.S)))
        # open economy parameters
        self.ss_firm_r_annual = self.world_int_rate
        self.ss_hh_r_annual = self.ss_firm_r_annual
        self.ss_firm_r = ((1 + self.ss_firm_r_annual) **
                          ((self.ending_age - self.starting_age) /
                           self.S) - 1)
        self.ss_hh_r = ((1 + self.ss_hh_r_annual) **
                        ((self.ending_age - self.starting_age) /
                         self.S) - 1)
        self.tpi_firm_r = np.ones(self.T+self.S) * self.ss_firm_r
        self.tpi_hh_r = np.ones(self.T+self.S) * self.ss_hh_r
        T_shift = np.concatenate((
            self.T_shifts, np.zeros((self.T + self.S -
                                     self.T_shifts.size, 1))))
        G_shift = np.concatenate((
            self.G_shifts, np.zeros((self.T - self.G_shifts.size, 1))))
        self.ALPHA_T = np.ones(self.T + self.S) * self.alpha_T + np.squeeze(T_shift)
        self.ALPHA_G = np.ones(self.T) * self.alpha_G + np.squeeze(G_shift)

        # set period of retirement
        # SHOULD BE UPDATED TO BE ENTERED AS Retirement age in defaults
        # then converted to model year here
        self.retire = np.int(np.round(9.0 * self.S / 16.0) - 1)

        # get population objects
        (self.omega, self.g_n_ss, self.omega_SS, self.surv_rate,
         self.rho, self.g_n, self.imm_rates,
         self.omega_S_preTP) = demographics.get_pop_objs(
                self.E, self.S, self.T, 1, 100, self.start_year,
                self.flag_graphs)

        # Interpolate chi_n and create omega_SS_80 if necessary
        if self.S == 80:
            self.omega_SS_80 = self.omega_SS
            self.chi_n = self.chi_n_80
        elif self.S < 80:
            self.age_midp_80 = np.linspace(20.5, 99.5, 80)
            self.chi_n_interp = si.interp1d(self.age_midp_80,
                                            np.squeeze(self.chi_n_80),
                                            kind='cubic')
            self.newstep = 80.0 / self.S
            self.age_midp_S = np.linspace(20 + 0.5 * self.newstep,
                                          100 - 0.5 * self.newstep,
                                          self.S)
            self.chi_n = self.chi_n_interp(self.age_midp_S)
            (_, _, self.omega_SS_80, _, _, _, _, _) = \
                demographics.get_pop_objs(20, 80, 320, 1, 100,
                                          self.start_year, False)
        self.e = income.get_e_interp(
            self.S, self.omega_SS, self.omega_SS_80, self.lambdas,
            plot=False)

    def get_tax_function_parameters(self, client, run_micro=False):
        # Income tax parameters
        if self.baseline:
            tx_func_est_path = os.path.join(
                self.output_base, 'TxFuncEst_baseline{}.pkl'.format(self.guid),
            )
        else:
            tx_func_est_path = os.path.join(
                self.output_base, 'TxFuncEst_policy{}.pkl'.format(self.guid),
            )
        if run_micro:
            txfunc.get_tax_func_estimate(
                self.BW, self.S, self.starting_age, self.ending_age,
                self.baseline, self.analytical_mtrs, self.tax_func_type,
                self.age_specific, self.start_year, self.reform, self.guid,
                tx_func_est_path, self.data, client, self.num_workers)
        if self.baseline:
            baseline_pckl = "TxFuncEst_baseline{}.pkl".format(self.guid)
            estimate_file = tx_func_est_path
            print('Using baseline tax parameters from ', tx_func_est_path)
            dict_params = self.read_tax_func_estimate(estimate_file,
                                                      baseline_pckl)

        else:
            policy_pckl = "TxFuncEst_policy{}.pkl".format(self.guid)
            estimate_file = tx_func_est_path
            print('Using reform policy tax parameters from ', tx_func_est_path)
            dict_params = self.read_tax_func_estimate(estimate_file,
                                                      policy_pckl)

        self.mean_income_data = dict_params['tfunc_avginc'][0]

        # Reorder indices of tax function and tile for all years after
        # budget window ends
        num_etr_params = dict_params['tfunc_etr_params_S'].shape[2]
        num_mtrx_params = dict_params['tfunc_mtrx_params_S'].shape[2]
        num_mtry_params = dict_params['tfunc_mtry_params_S'].shape[2]
        self.etr_params = np.empty((self.T, self.S, num_etr_params))
        self.mtrx_params = np.empty((self.T, self.S, num_mtrx_params))
        self.mtry_params = np.empty((self.T, self.S, num_mtry_params))
        self.etr_params[:self.BW, :, :] =\
            np.transpose(
                dict_params['tfunc_etr_params_S'][:self.S, :self.BW, :],
                axes=[1, 0, 2])
        self.etr_params[self.BW:, :, :] =\
            np.tile(np.transpose(
                dict_params['tfunc_etr_params_S'][:self.S, -1, :].reshape(
                    self.S, 1, num_etr_params), axes=[1, 0, 2]),
                    (self.T - self.BW, 1, 1))
        self.mtrx_params[:self.BW, :, :] =\
            np.transpose(
                dict_params['tfunc_mtrx_params_S'][:self.S, :self.BW, :],
                axes=[1, 0, 2])
        self.mtrx_params[self.BW:, :, :] =\
            np.transpose(
                dict_params['tfunc_mtrx_params_S'][:self.S, -1, :].reshape(
                    self.S, 1, num_mtrx_params), axes=[1, 0, 2])
        self.mtry_params[:self.BW, :, :] =\
            np.transpose(
                dict_params['tfunc_mtry_params_S'][:self.S, :self.BW, :],
                axes=[1, 0, 2])
        self.mtry_params[self.BW:, :, :] =\
            np.transpose(
                dict_params['tfunc_mtry_params_S'][:self.S, -1, :].reshape(
                    self.S, 1, num_mtry_params), axes=[1, 0, 2])

        if self.constant_rates:
            print('Using constant rates!')
            # # Make all ETRs equal the average
            self.etr_params = np.zeros(self.etr_params.shape)
            # set shift to average rate
            self.etr_params[:, :, 10] = dict_params['tfunc_avg_etr']

            # # Make all MTRx equal the average
            self.mtrx_params = np.zeros(self.mtrx_params.shape)
            # set shift to average rate
            self.mtrx_params[:, :, 10] = dict_params['tfunc_avg_mtrx']

            # # Make all MTRy equal the average
            self.mtry_params = np.zeros(self.mtry_params.shape)
            # set shift to average rate
            self.mtry_params[:, :, 10] = dict_params['tfunc_avg_mtry']

    def read_tax_func_estimate(self, pickle_path, pickle_file):
        '''
        --------------------------------------------------------------------
        This function reads in tax function parameters
        --------------------------------------------------------------------

        INPUTS:
        pickle_path = string, path to pickle with tax function parameter
                      estimates
        pickle_file = string, name of pickle file with tax function
                      parmaeter estimates

        OTHER FUNCTIONS AND FILES CALLED BY THIS FUNCTION:
        /picklepath/ = pickle file with dictionary of tax function
                       estimated parameters

        OBJECTS CREATED WITHIN FUNCTION:
        dict_params = dictionary, contains numpy arrays of tax function
                      estimates

        RETURNS: dict_params
        --------------------------------------------------------------------
        '''
        if os.path.exists(pickle_path):
            print('pickle path exists')
            with open(pickle_path, 'rb') as pfile:
                try:
                    dict_params = pickle.load(pfile, encoding='latin1')
                except TypeError:
                    dict_params = pickle.load(pfile)
        else:
            from pkg_resources import resource_stream, Requirement
            path_in_egg = pickle_file
            pkl_path = os.path.join(os.path.dirname(__file__), '..',
                                    path_in_egg)
            with open(pkl_path, 'rb') as pfile:
                try:
                    dict_params = pickle.load(pfile, encoding='latin1')
                except TypeError:
                    dict_params = pickle.load(pfile)

        return dict_params

    def default_parameters(self):
        """
        Return Policy object same as self except with current-law policy.
        Returns
        -------
        Specifications: Specifications instance with the default configuration
        """
        dp = Specifications()
        return dp

    def update_specifications(self, revision, raise_errors=True):
        """
        Updates parameter specification with values in revision dictionary
        Parameters
        ----------
        reform: dictionary of one or more PARAM:VALUE pairs
        raise_errors: boolean
            if True (the default), raises ValueError when parameter_errors
                    exists;
            if False, does not raise ValueError when parameter_errors exists
                    and leaves error handling to caller of
                    update_specifications.
        Raises
        ------
        ValueError:
            if raise_errors is True AND
              _validate_parameter_names_types generates errors OR
              _validate_parameter_values generates errors.
        Returns
        -------
        nothing: void
        Notes
        -----
        Given a reform dictionary, typical usage of the Policy class
        is as follows::
            specs = Specifications()
            specs.update_specifications(reform)
        An example of a multi-parameter specification is as follows::
            spec = {
                frisch: [0.03]
            }
        This method was adapted from the Tax-Calculator
        behavior.py-update_behavior method.
        """
        # check that all revisions dictionary keys are integers
        if not isinstance(revision, dict):
            raise ValueError('ERROR: revision is not a dictionary')
        if not revision:
            return  # no revision to implement
        revision_years = sorted(list(revision.keys()))
        # check range of remaining revision_years
        # validate revision parameter names and types
        self.parameter_errors = ''
        self.parameter_warnings = ''
        self._validate_parameter_names_types(revision)
        if not self._ignore_errors and self.parameter_errors:
            raise ValueError(self.parameter_errors)
        # implement the revision
        revision_parameters = set()
        revision_parameters.update(revision.keys())
        self._update(revision)
        # validate revision parameter values
        self._validate_parameter_values(revision_parameters)
        if self.parameter_errors and raise_errors:
            raise ValueError('\n' + self.parameter_errors)
        self.compute_default_params()

    @staticmethod
    def read_json_param_objects(revision):
        """
        Read JSON file and convert to dictionary
        Returns
        -------
        rev_dict: formatted dictionary
        """
        # next process first reform parameter
        if revision is None:
            rev_dict = dict()
        elif isinstance(revision, six.string_types):
            if os.path.isfile(revision):
                txt = open(revision, 'r').read()
            else:
                txt = revision
            # strip out //-comments without changing line numbers
            json_str = re.sub('//.*', ' ', txt)
            # convert JSON text into a Python dictionary
            try:
                rev_dict = json.loads(json_str)
            except ValueError as valerr:
                msg = 'Policy reform text below contains invalid JSON:\n'
                msg += str(valerr) + '\n'
                msg += 'Above location of the first error may be approximate.\n'
                msg += 'The invalid JSON reform text is between the lines:\n'
                bline = 'XX----.----1----.----2----.----3----.----4'
                bline += '----.----5----.----6----.----7'
                msg += bline + '\n'
                linenum = 0
                for line in json_str.split('\n'):
                    linenum += 1
                    msg += '{:02d}{}'.format(linenum, line) + '\n'
                msg += bline + '\n'
                raise ValueError(msg)
        else:
            raise ValueError('reform is neither None nor string')

        return rev_dict

    def _validate_parameter_names_types(self, revision):
        """
        Check validity of parameter names and parameter types used
        in the specified revision dictionary.
        Parameters
        ----------
        revision: parameter dictionary of form {parameter_name: [value]}
        Returns:
        --------
        nothing: void
        Notes
        -----
        copied from taxcalc.Behavior._validate_parameter_names_types
        """
        param_names = set(self._vals.keys())
        # print('Parameter names = ', param_names)
        revision_param_names = list(revision.keys())
        for param_name in revision_param_names:
            if param_name not in param_names:
                msg = '{} unknown parameter name'
                self.parameter_errors += (
                    'ERROR: ' + msg.format(param_name) + '\n'
                )
            else:
                # check parameter value type avoiding use of isinstance
                # because isinstance(True, (int,float)) is True, which
                # makes it impossible to check float parameters
                bool_param_type = self._vals[param_name]['boolean_value']
                int_param_type = self._vals[param_name]['integer_value']
                string_param_type = self._vals[param_name]['string_value']
                # make scalars into single item lists
                if isinstance(revision[param_name], list):
                    param_value = revision[param_name]
                else:
                    param_value = [revision[param_name]]
                for idx in range(0, len(param_value)):
                    pval = param_value[idx]
                    pval_is_bool = type(pval) == bool
                    pval_is_int = type(pval) == int
                    pval_is_float = type(pval) == float
                    pval_is_string = type(pval) == str
                    if bool_param_type:
                        if not pval_is_bool:
                            msg = '{} value {} is not boolean'
                            self.parameter_errors += (
                                'ERROR: ' +
                                msg.format(param_name, pval) +
                                '\n'
                            )
                    elif int_param_type:
                        if not pval_is_int:  # pragma: no cover
                            msg = '{} value {} is not integer'
                            self.parameter_errors += (
                                'ERROR: ' +
                                msg.format(param_name, pval) +
                                '\n'
                            )
                    elif string_param_type:
                        if not pval_is_string:  # pragma: no cover
                            msg = '{} value {} is not string'
                            self.parameter_errors += (
                                'ERROR: ' +
                                msg.format(param_name, pval) +
                                '\n'
                            )
                    else:  # param is float type
                        if not (pval_is_int or pval_is_float):
                            msg = '{} value {} is not a number'
                            self.parameter_errors += (
                                'ERROR: ' +
                                msg.format(param_name, pval) +
                                '\n'
                            )
        del param_names

    def _validate_parameter_values(self, parameters_set):
        """
        Check values of parameters in specified parameter_set using
        range information from the current_law_policy.json file.
        Parameters:
        -----------
        parameters_set: set of parameters whose values need to be validated
        Returns:
        --------
        nothing: void
        Notes
        -----
        copied from taxcalc.Policy._validate_parameter_values
        """
        dp = self.default_parameters()
        parameters = sorted(parameters_set)
        for param_name in parameters:
            param_value = getattr(self, param_name)
            if not hasattr(param_value, 'shape'):  # value is not a numpy array
                param_value = np.array([param_value])
            for validation_op, validation_value in self._vals[param_name]['range'].items():
                if validation_op == 'possible_values':
                    if param_value not in validation_value:
                        out_of_range = True
                        msg = '{} value {} not in possible values {}'
                        if out_of_range:
                            self.parameter_errors += (
                                'ERROR: ' + msg.format(param_name,
                                                       param_value,
                                                       validation_value) + '\n'
                                )
                else:
                    print(validation_op, param_value, validation_value)
                    if isinstance(validation_value, six.string_types):
                        validation_value = self.simple_eval(validation_value)
                    else:
                        validation_value = np.full(param_value.shape,
                                                   validation_value)
                    assert param_value.shape == validation_value.shape
                    for idx in np.ndindex(param_value.shape):
                        out_of_range = False
                        if validation_op == 'min' and (param_value[idx] <
                                                       validation_value[idx]):
                            out_of_range = True
                            msg = '{} value {} < min value {}'
                            extra = self._vals[param_name]['out_of_range_minmsg']
                            if extra:
                                msg += ' {}'.format(extra)
                        if validation_op == 'max' and (param_value[idx] >
                                                       validation_value[idx]):
                            out_of_range = True
                            msg = '{} value {} > max value {}'
                            extra = self._vals[param_name]['out_of_range_maxmsg']
                            if extra:
                                msg += ' {}'.format(extra)
                        if out_of_range:
                            self.parameter_errors += (
                                'ERROR: ' + msg.format(
                                    param_name, param_value[idx],
                                    validation_value[idx]) + '\n')
        del dp
        del parameters


# copied from taxcalc.tbi.tbi.reform_errors_warnings--probably needs further
# changes
def reform_warnings_errors(user_mods):
    """
    Generate warnings and errors for OG-USA parameter specifications
    Parameters:
    -----------
    user_mods : dict created by read_json_param_objects
    Return
    ------
    rtn_dict : dict with endpoint specific warning and error messages
    """
    rtn_dict = {'ogusa': {'warnings': '', 'errors': ''}}

    # create Specifications object and implement reform
    specs = Specifications()
    specs._ignore_errors = True
    try:
        specs.update_specifications(user_mods['ogusa'], raise_errors=False)
        rtn_dict['ogusa']['warnings'] = specs.parameter_warnings
        rtn_dict['ogusa']['errors'] = specs.parameter_errors
    except ValueError as valerr_msg:
        rtn_dict['ogusa']['errors'] = valerr_msg.__str__()
    return rtn_dict
