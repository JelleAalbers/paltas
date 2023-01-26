# -*- coding: utf-8 -*-
"""
Draw subhalo masses and concentrations for NFW subhalos according to
https://arxiv.org/pdf/1909.02573.pdf

This module contains the functions needed to turn the parameters of NFW
subhalo distributions into masses, concentrations, and positions for those
NFW subhalos.
"""
import numba
import numpy as np

import paltas

from .subhalos_base import SubhalosBase
from . import nfw_functions, dg19_utils
from ..Utils import power_law, cosmology_utils


class SubhalosDG19(SubhalosBase):
    """Class for rendering the subhalos of a main halos according to DG19.

    Args:
        subhalo_parameters (dict): A dictionary containing the type of
            subhalo distribution and the value for each of its parameters.
        main_deflector_parameters (dict): A dictionary containing the type of
            main deflector and the value for each of its parameters.
        source_parameters (dict): A dictionary containing the type of the
            source and the value for each of its parameters.
        cosmology_parameters (str,dict, or colossus.cosmology.Cosmology):
            Either a name of colossus cosmology, a dict with 'cosmology name':
            name of colossus cosmology, an instance of colussus cosmology, or a
            dict with H0 and Om0 ( other parameters will be set to defaults).

    Notes:

    Required Parameters

    - sigma_sub - SHMF normalization in units of kpc^(-2)
    - shmf_plaw_index - SHMF mass function power-law slope
    - m_pivot - SHMF power-law pivot mass in unit of M_sun
    - m_min - SHMF minimum rendered mass in units of M_sun
    - m_max - SHMF maximum rendered mass in units of M_sun
    - c_0 - concentration normalization
    - conc_zeta - concentration redshift power law slope
    - conc_beta - concentration peak height power law slope
    - conc_m_ref - concentration peak height pivot mass
    - dex_scatter - scatter in concentration in units of dex
    - k1 - slope of SHMF host mass dependence
    - k2 - slope of SHMF host redshift dependence
    """
    # Define the parameters we expect to find for the DG_19 model
    required_parameters = ('sigma_sub','shmf_plaw_index','m_pivot','m_min',
        'm_max','c_0','conc_zeta','conc_beta','conc_m_ref','dex_scatter',
        'k1','k2')

    def __init__(self,subhalo_parameters,main_deflector_parameters,
        source_parameters,cosmology_parameters):

        # Initialize the super class
        super().__init__(subhalo_parameters,main_deflector_parameters,
            source_parameters,cosmology_parameters)

    @staticmethod
    @numba.njit()
    def host_scaling_function(host_m200, z_lens, k1=0.88, k2=1.7):
        """Returns scaling for the subhalo mass function based on the mass of
        the host halo.

        Derived from galacticus in https://arxiv.org/pdf/1909.02573.pdf.

        Args:
            host_m200 (float): The mass of the host halo in units of M_sun
            z_lens (flat): The redshift of the host halo / main deflector
            k1 (flaot): Amplitude of halo mass dependence
            k2 (flaot): Amplitude of the redshift scaling

        Returns:
            (float): The normalization scaling for the subhalo mass function

        Notes:
            Default values of k1 and k2 are derived from galacticus.
        """
        # Equation from DG_19
        log_f = k1 * np.log10(host_m200/1e13) + k2 * np.log10(z_lens+0.5)
        return 10**log_f

    def draw_nfw_masses(self):
        """Draws from the https://arxiv.org/pdf/1909.02573.pdf subhalo mass
        function and returns an array of the masses.

        Returns:
            (np.array): The masses of the drawn halos in units of M_sun
        """

        # Pull the parameters we need from the input dictionaries
        # Units of m_sun times inverse kpc^2
        sigma_sub = max(0, self.subhalo_parameters['sigma_sub'])
        shmf_plaw_index = self.subhalo_parameters['shmf_plaw_index']
        # Units of m_sun
        m_pivot = self.subhalo_parameters['m_pivot']
        # Units of m_sun
        host_m200 = self.main_deflector_parameters['M200']
        # Units of m_sun
        m_min = self.subhalo_parameters['m_min']
        # Units of m_sun
        m_max = self.subhalo_parameters['m_max']
        z_lens = self.main_deflector_parameters['z_lens']
        k1 = self.subhalo_parameters['k1']
        k2 = self.subhalo_parameters['k2']

        # Calculate the overall norm of the power law. This includes host
        # scaling, sigma_sub, and the area of interest.
        f_host = self.host_scaling_function(host_m200,z_lens,k1=k1,k2=k2)

        # In DG_19 subhalos are rendered up until 3*theta_E.
        # Colossus return in MPC per h per radian so must be converted to kpc
        # per arc second
        kpc_per_arcsecond = cosmology_utils.kpc_per_arcsecond(z_lens,
            self.cosmo)
        r_E = (kpc_per_arcsecond*self.main_deflector_parameters['theta_E'])
        dA = np.pi * (3*r_E)**2

        # We can also fold in the pivot mass into the norm for simplicity (then
        # all we have to do is sample from a power law).
        norm = f_host*dA*sigma_sub*m_pivot**(-shmf_plaw_index-1)

        # Draw from our power law and return the masses.
        masses = power_law.power_law_draw(m_min,m_max,shmf_plaw_index,norm)
        return masses

    def mass_concentration(self,z,m_200):
        """Returns the concentration of halos at a certain mass given the
        parameterization of DG_19.

        Args:
            z (np.array): The redshift of the nfw halo
            m_200 (np.array): array of M_200 of the nfw halo units of M_sun

        Returns:
            (np.array): The concentration for each halo.
        """
        return dg19_utils.mass_concentration(
            parameter_dict=self.subhalo_parameters,
            cosmo=self.cosmo,
            z=z,
            m_200=m_200)

    @staticmethod
    def rejection_sampling(r_samps,r_200,r_3E):
        """Given the radial sampling of the positions and DG_19 constraints,
        conducts rejection sampling and return the cartesian positions.

        Args:
            r_samps (np.array): Samples of the radial coordinates for
                the subhalos in units of kpc.
            r_200 (float): The r_200 of the host halo which will be used
                as the maximum z magnitude in units of kpc.
            r_3E (float): 3 times the einstein radius, which will be used
                to bound the x and y coordinates in units of kpc.

        Returns:
            ([np.array,...]): A list of two numpy arrays: the boolean
            array of accepted samples and a n_subsx3 array of x,y,z
            coordinates. All in units of kpc.
        """
        # Sample theta and phi values for all of the radii samples
        theta = np.random.rand(len(r_samps)) * 2 * np.pi
        phi = np.arccos(1-2*np.random.rand(len(r_samps)))

        # Initialize the x,y,z array
        cart_pos = np.zeros(r_samps.shape+(3,))

        # Get the x, y, and z coordinates
        cart_pos[:,0] += r_samps*np.sin(phi)*np.cos(theta)
        cart_pos[:,1] += r_samps*np.sin(phi)*np.sin(theta)
        cart_pos[:,2] += r_samps*np.cos(phi)

        # Test which samples are outside the DG_19 bounds
        r2_inside = np.sqrt(cart_pos[:,0]**2+cart_pos[:,1]**2)<r_3E
        z_inside = np.abs(cart_pos[:,2])<r_200
        keep = np.logical_and(r2_inside,z_inside)

        return (keep,cart_pos)

    def sample_cored_nfw(self,n_subs):
        """Given the a tidal radius that defines a core region and the
        parameters of the main deflector, samples positions for NFW subhalos
        bounded as described in https://arxiv.org/pdf/1909.02573.pdf

        Args:
            n_subs (int): The number of subhalo positions to sample

        Returns:
            (np.array): A n_subs x 3 array giving the x,y,z position of the
            subhalos in units of kpc.

        Notes:
            The code works through rejection sampling, which can be inneficient
            for certain configurations. If this is a major issue, it may be
            worth introducing more analytical components.
        """

        # Create an array that will store our coordinates
        cart_pos = np.zeros((n_subs,3))

        # Calculate the needed host properties
        host_m200 = self.main_deflector_parameters['M200']
        z_lens = self.main_deflector_parameters['z_lens']
        host_c = self.mass_concentration(z_lens,host_m200)
        host_r_200 = nfw_functions.r_200_from_m(host_m200,z_lens,self.cosmo)
        host_r_scale = host_r_200/host_c
        # DG_19 definition of the tidal radius
        r_tidal = host_r_200/2
        host_rho_nfw = nfw_functions.rho_nfw_from_m_c(host_m200,host_c,
            self.cosmo,r_scale=host_r_scale)

        # Tranform the einstein radius to physical units (TODO this should
        # be a function). Multiply by 3 since that's what's relevant for
        # DG_19 parameterization.
        kpc_per_arcsecond = cosmology_utils.kpc_per_arcsecond(z_lens,self.cosmo)
        r_3E = (kpc_per_arcsecond*self.main_deflector_parameters['theta_E'])*3

        # The largest radius we should bother sampling is set by the diagonal of
        # our cylinder.
        r_max = np.sqrt(r_3E**2+host_r_200**2)

        n_accepted_draws = 0
        r_subs = nfw_functions.cored_nfw_draws(r_tidal,host_rho_nfw,
            host_r_scale,r_max,n_subs)
        keep_ind, cart_draws = self.rejection_sampling(r_subs,host_r_200,r_3E)

        # Save the cartesian coordinates we want to keep
        cart_pos[n_accepted_draws:n_accepted_draws+np.sum(keep_ind)] = (
            cart_draws[keep_ind])
        n_accepted_draws += np.sum(keep_ind)

        # Get the fraction of rejection to see how much we should sample
        rejection_frac = max(1-np.mean(keep_ind),1e-1)

        # Keep drawing until we have enough r_subs.
        while n_accepted_draws<n_subs:
            r_subs = nfw_functions.cored_nfw_draws(r_tidal,host_rho_nfw,
                host_r_scale,r_max,int(np.round(n_subs*rejection_frac)))
            keep_ind, cart_draws = self.rejection_sampling(r_subs,host_r_200,
                r_3E)
            use_keep = np.minimum(n_subs-n_accepted_draws,np.sum(keep_ind))
            # Save the cartesian coordinates we want to keep
            cart_pos[n_accepted_draws:n_accepted_draws+use_keep] = (
                cart_draws[keep_ind][:use_keep])
            n_accepted_draws += use_keep

        return cart_pos

    @staticmethod
    def get_truncation_radius(m_200,r,m_pivot=1e7,r_pivot=50):
        """Returns the truncation radius for a subhalo given the mass and
        radial position in the host NFW

        Args:
            m_200 (np.array): The mass of the subhalos in units of M_sun
            r (np.array): The radial position of the subhalos in units of kpc
            m_pivot (float): The pivot mass for the scaling in units of M_sun
            r_pivot (float): The pivot radius for the relation in unit of kpc

        Returns:
            (np.array): The truncation radii for the subhalos in units of kpc
        """

        return 1.4*(m_200/m_pivot)**(1/3)*(r/r_pivot)**(2/3)

    def convert_to_lenstronomy(self,subhalo_masses,subhalo_cart_pos):
        """Converts the subhalo masses and position to truncated NFW profiles
        for lenstronomy

        Args:
            subhalo_masses (np.array): The masses of each of the subhalos that
                were drawn
            subhalo_cart_pos (np.array): A n_subs x 3D array of the positions
                of the subhalos that were drawn
        Returns:
            ([string,...],[dict,...]): A tuple containing the list of models
            and the list of kwargs for the truncated NFWs.
        """
        # First, for each subhalo mass we'll also have to draw a concentration.
        # This requires a redshift. DG_19 used the predicted redshift of infall
        # from galacticus. For now, we'll use the redshift of the lens itself.
        # TODO: Use a different redshift
        z_lens = self.main_deflector_parameters['z_lens']
        z_source = self.source_parameters['z_source']
        subhalo_z = (np.ones(subhalo_masses.shape) *
            self.main_deflector_parameters['z_lens'])
        concentration = self.mass_concentration(subhalo_z,subhalo_masses)

        # We'll also need the radial position in the halo
        r_in_host = np.sqrt(np.sum(subhalo_cart_pos**2,axis=-1))

        # Now we can convert these masses and concentrations into NFW parameters
        # for lenstronomy.
        sub_r_200 = nfw_functions.r_200_from_m(subhalo_masses,subhalo_z,
            self.cosmo)
        sub_r_scale = sub_r_200/concentration
        sub_rho_nfw = nfw_functions.rho_nfw_from_m_c(subhalo_masses,
            concentration,self.cosmo,
            r_scale=sub_r_scale)
        sub_r_trunc = self.get_truncation_radius(subhalo_masses,r_in_host)

        # Convert to lenstronomy units
        sub_r_scale_ang, alpha_Rs, sub_r_trunc_ang = (
            nfw_functions.convert_to_lenstronomy_tNFW(
                sub_r_scale,subhalo_z,sub_rho_nfw,sub_r_trunc,z_source,
                self.cosmo))
        kpc_per_arcsecond = cosmology_utils.kpc_per_arcsecond(z_lens,
            self.cosmo)
        cart_pos_ang = subhalo_cart_pos / np.expand_dims(kpc_per_arcsecond,
            axis=-1)

        # Populate the parameters for each lens
        model_list = []
        kwargs_list = []
        for i in range(len(subhalo_masses)):
            model_list.append('TNFW')
            kwargs_list.append({'alpha_Rs':alpha_Rs[i],'Rs':sub_r_scale_ang[i],
                'center_x':(cart_pos_ang[i,0]+
                    self.main_deflector_parameters['center_x']),
                'center_y':(cart_pos_ang[i,1]+
                    self.main_deflector_parameters['center_y']),
                'r_trunc':sub_r_trunc_ang[i]})

        return (model_list,kwargs_list)

    def draw_subhalos(self):
        """Draws masses, concentrations,and positions for the subhalos of a
        main lens halo.

        Returns:
            (tuple): A tuple of three lists: the first is the profile type for
            each subhalo returned, the second is the lenstronomy kwargs for
            that subhalo, and the third is the redshift for each subhalo.
        Notes:
            The redshift for each subhalo is the same as the host, so the
            returned redshift list is not necessary unless the output is
            being combined with los substructure.
        """
        # Stupid wrapper for compatibility with the test suite
        from paltas.Configs.config_handler import LenstronomyInputs
        result = LenstronomyInputs()
        self.draw(result)
        return (
            result.kwargs_model['lens_model_list'], 
            result.kwargs_params['kwargs_lens'], 
            result.kwargs_model['lens_redshift_list'])

    def draw(self, result, **kwargs):
        # Distribute subhalos according to https://arxiv.org/pdf/1909.02573.pdf
        # DG_19 assumes NFWs distributed throughout the main deflector.
        # For these NFWs we need positions, masses, and concentrations that
        # we will then translate to Lenstronomy parameters.
        subhalo_masses = self.draw_nfw_masses()

        # If we have no subhalos, we have nothing to add to the results
        if subhalo_masses.size == 0:
            return

        subhalo_cart_pos = self.sample_cored_nfw(len(subhalo_masses))
        model_list, kwargs_list = self.convert_to_lenstronomy(
            subhalo_masses,subhalo_cart_pos)
        redshift_list = [self.main_deflector_parameters['z_lens']] * len(subhalo_masses)

        add_galaxies_in_subhalos(
            result, subhalo_masses, kwargs_list, 
            subhalo_redshifts=redshift_list,
            subhalo_parameters=self.subhalo_parameters,
            source_parameters=self.source_parameters,
            cosmo=self.cosmo)

        result.add_lenses(
            models=model_list, 
            model_kwargs=kwargs_list,
            redshifts=redshift_list)


def add_galaxies_in_subhalos(
        result, subhalo_masses, subhalo_kwargs, subhalo_redshifts, 
        subhalo_parameters: dict, source_parameters: dict, 
        cosmo, 
        tidal_stripping=True,
        _print_max=True):

    model_list = []
    kwargs_list = []
    ghc_config = {
        param_name[len('ghc_'):]: value 
        for param_name, value in subhalo_parameters.items()
        if param_name.startswith('ghc_')}

    max_subhalo_i = np.argmax(subhalo_masses)

    for subhalo_i, (mass, subh, redshift) in enumerate(zip(
            subhalo_masses, subhalo_kwargs, subhalo_redshifts)):
        # Do not paint galaxies onto subhalos below a threshold
        # (by default, add no galaxies in subhalos)
        if mass < ghc_config.get('m_min', np.inf):
            continue

        sersic_params = dict(
            # Center the galaxy in the subhalo
            center_x=subh['center_x'], 
            center_y=subh['center_y'],
            # ~1 seems reasonable, see ApJ 874:29, 2019
            n_sersic=ghc_config.get('sersic_index', 1),
        )

        if tidal_stripping:
            # Convert M200 to MPeak based on figure B1 of ApJ 915:116
            # Cotroborated by p.3 of ApJ 917 7: difference is roughly a factor 2
            mass *= 2 * 10**(0.2 * np.random.randn())

        # Convert halo mass to stellar mass
        # Power law indices to roughly match Figure 4 of ApJ 915:116
        ghc_reference_mass = ghc_config.get('reference_mass', 6e6)
        ghc_plaw_index = ghc_config.get('plaw_index', 2)
        ghc_scatter_dex = ghc_config.get('scatter_dex', 0.5)   # Eyeball
        mass_stars = (mass / ghc_reference_mass)**ghc_plaw_index
        mass_stars *= 10**(ghc_scatter_dex * np.random.randn())

        # Stellar mass (/Msun) to luminosity (/Lsun)
        # See Figure 7 of Wei Du et al 2020 AJ 159 138
        # and note F81W is very very roughly i, or maybe z, so
        # log10([M/Msun]/ [L/Lsun]) ~ 0.4 +- 0.2
        ghc_gamma_star = ghc_config.get('gamma_star', 0.4)
        ghc_gamma_star_scatter = ghc_config.get('gamma_star_scatter', 0.2)
        luminosity = mass_stars / 10**(
            ghc_gamma_star + ghc_gamma_star_scatter * np.random.randn())

        # Luminosty (/Lsun) to absolute magnitude
        # Using the bolometric magnitude here, seems reasonably since gamma*
        # doesn't depend much on color?
        mag_absolute = ghc_config.get('mabs_sun', 4.74) - 2.5 * np.log10(luminosity)

        # Absolute magnitude to half-light radius
        ghc_rhalf_m_c = ghc_config.get('rhalf_mag_c', 15.886)
        ghc_rhalf_m_a = ghc_config.get('rhalf_mag_a', -0.312)
        ghc_rhalf_m_dex = ghc_config.get('rhalf_scatter_dex', 0.4)
        rhalf_physical_parsec = ghc_rhalf_m_c * np.exp(ghc_rhalf_m_a * mag_absolute)
        rhalf_physical_parsec *= 10**(ghc_rhalf_m_dex * np.random.randn())

        # angle in radians ~= r_physical / d_angulardiam
        radian_to_arcsec = 3600 * 180/np.pi
        sersic_params['R_sersic'] = radian_to_arcsec * rhalf_physical_parsec / (
            # Get Mpc/h from cosmosis, want pc -> * 1e6/h
            cosmo.angularDiameterDistance(redshift)
            * 1e6/cosmo.h)

        # Convert the magnitude to lenstronomy's arcane "amp" parameter
        # Some duplication with SingleSersicSource here
        mag_apparent = cosmology_utils.absolute_to_apparent(
            mag_absolute=mag_absolute,
            z_light=redshift,
            cosmo=cosmo,
            include_k_correction=source_parameters.get('include_k_corrections', True))
        sersic_params['amp'] = paltas.Sources.sersic.SingleSersicSource.mag_to_amplitude(
            mag_apparent,
            source_parameters['output_ab_zeropoint'],
            sersic_params) * ghc_config.get('luminosity_multiplier', 1)

        if _print_max and subhalo_i == max_subhalo_i:
            print(
                f"Halo mass {mass:.3g},\tmstar {mass_stars:.3g},\t"
                f"Mabs {mag_absolute:.3g},\trhalf {rhalf_physical_parsec:.3g},\t"
                f"mapp {mag_apparent:.3g}, "
                f"y(really x) {subh['center_y']:.2f}, " 
                f"x(really y) {subh['center_x']:.2f}")

        model_list.append('SERSIC')
        kwargs_list.append(sersic_params)
        
    result.add_lens_light(
        models=model_list,
        model_kwargs=kwargs_list
    )
