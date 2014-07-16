#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Compute
~~~~~~~

Methods for computing concepts, constellations, and integrated information of
subsystems.
"""

import functools
import numpy as np
from joblib import Parallel, delayed
from scipy.sparse.csgraph import connected_components
from scipy.sparse import csr_matrix

from . import utils, constants, memory
from .concept_caching import concept as _concept
from .models import Concept, Cut, BigMip
from .network import Network
from .subsystem import Subsystem
from .lru_cache import lru_cache


# TODO update concept docs
def concept(subsystem, mechanism):
    """Return the concept specified by the a mechanism within a subsytem.

    Args:
        subsystem (Subsytem): The context in which the mechanism should be
            considered.
        mechanism (tuple(Node)): The candidate set of nodes.

    Returns:
        ``Concept`` -- The pair of maximally irreducible cause/effect
        repertoires that constitute the concept specified by the given
        mechanism, or ``None`` if there isn't one.

    .. note::
        The output is persistently cached to avoid recomputation. See the
        documentation for :mod:`cyphi.concept_caching`.
    """
    # Pre-checks:
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    # If the mechanism is empty, there is no concept.
    if not mechanism:
        return subsystem.null_concept
    # If any node in the mechanism either has no inputs from the subsystem or
    # has no outputs to the subsystem, then the mechanism is necessarily
    # reducible and cannot be a concept (since removing that node would make no
    # difference to at least one of the MICEs).
    if not (subsystem._all_connect_to_any(mechanism, subsystem.nodes) and
            subsystem._any_connect_to_all(subsystem.nodes, mechanism)):
        return Concept(mechanism=mechanism, phi=0.0, cause=None, effect=None)
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    # Passed prechecks; pass it over to the concept caching logic.
    return _concept(subsystem, mechanism)


def constellation(subsystem):
    """Return the conceptual structure of this subsystem.

    Args:
        subsystem (Subsytem): The subsystem for which to determine the
            constellation.

    Returns:
        ``tuple(Concept)`` -- A tuple of all the Concepts in the constellation.
    """
    concepts = [concept(subsystem, mechanism) for mechanism in
                utils.powerset(subsystem.nodes)]
    # Filter out non-concepts, i.e. those with effectively zero Phi.
    return tuple(filter(None, concepts))


@lru_cache(maxmem=constants.MAXIMUM_CACHE_MEMORY_PERCENTAGE)
def concept_distance(c1, c2):
    """Return the distance between two concepts in concept-space.

    Args:
        c1 (Mice): The first concept.
        c2 (Mice): The second concept.

    Returns:
        ``float`` -- The distance between the two concepts in concept-space.
    """
    # Calculate the sum of the past and future EMDs, expanding the repertoires
    # to the full state-space of the subsystem, so that the EMD signatures are
    # the same size.
    return sum([
        utils.hamming_emd(c1.expand_cause_repertoire(),
                          c2.expand_cause_repertoire()),
        utils.hamming_emd(c1.expand_effect_repertoire(),
                          c2.expand_effect_repertoire())])


def _constellation_distance_simple(C1, C2, subsystem):
    """Return the distance between two constellations in concept-space,
    assuming the only difference between them is that some concepts have
    disappeared."""
    # Make C1 refer to the bigger constellation
    if len(C2) > len(C1):
        C1, C2 = C2, C1
    destroyed = [c for c in C1 if c not in C2]
    return sum(c.phi * concept_distance(c, subsystem.null_concept)
               for c in destroyed)


def _constellation_distance_emd(C1, C2, unique_C1, unique_C2, subsystem):
    """Return the distance between two constellations in concept-space,
    using the generalized EMD."""
    shared_concepts = [c for c in C1 if c in C2]
    # Construct null concept and list of all unique concepts.
    all_concepts = (shared_concepts + unique_C1 + unique_C2 +
                    [subsystem.null_concept])
    # Construct the two phi distributions.
    d1, d2 = [[c.phi if c in constellation else 0 for c in all_concepts]
              for constellation in (C1, C2)]
    # Calculate how much phi disappeared and assign it to the null concept
    # (the null concept is the last element in the distribution).
    residual = sum(d1) - sum(d2)
    if residual > 0:
        d2[-1] = residual
    if residual < 0:
        d1[-1] = residual
    # Generate the ground distance matrix.
    distance_matrix = np.array([
        [concept_distance(i, j) for i in all_concepts] for j in
        all_concepts])

    return utils.emd(np.array(d1), np.array(d2), distance_matrix)


@lru_cache(maxmem=constants.MAXIMUM_CACHE_MEMORY_PERCENTAGE)
def constellation_distance(C1, C2, subsystem):
    """Return the distance between two constellations in concept-space.

    Args:
        C1 (tuple(Concept)): The first constellation.
        C2 (tuple(Concept)): The second constellation.
        null_concept (Concept): The null concept of a candidate set, *i.e* the
            "origin" of the concept space in which the given constellations
            reside.

    Returns:
        ``float`` -- The distance between the two constellations in
        concept-space.
    """
    concepts_only_in_C1 = [c for c in C1 if c not in C2]
    concepts_only_in_C2 = [c for c in C2 if c not in C1]
    # If the only difference in the constellations is that some concepts
    # disappeared, then we don't need to use the EMD.
    if not concepts_only_in_C1 or not concepts_only_in_C2:
        return _constellation_distance_simple(C1, C2, subsystem)
    else:
        return _constellation_distance_emd(C1, C2,
                                           concepts_only_in_C1,
                                           concepts_only_in_C2,
                                           subsystem)


def conceptual_information(subsystem):
    """Return the conceptual information for a subsystem.

    This is the distance from the subsystem's constellation to the null
    concept."""
    return constellation_distance(constellation(subsystem), (), subsystem)


# TODO document
def _null_mip(subsystem):
    """Returns a BigMip with zero phi and empty constellations.

    This is the MIP associated with a reducible subsystem."""
    return BigMip(subsystem=subsystem,
                  phi=0.0,
                  unpartitioned_constellation=[], partitioned_constellation=[])


def _single_node_mip(subsystem):
    """Returns a the BigMip of a single-node with a selfloop.

    Whether these have a nonzero |Phi| value depends on the CyPhi constants."""
    if constants.SINGLE_NODES_WITH_SELFLOOPS_HAVE_PHI:
        # TODO return the actual concept
        return BigMip(
            phi=0.5,
            unpartitioned_constellation=None,
            partitioned_constellation=None,
            subsystem=subsystem)
    else:
        return _null_mip(subsystem)


# TODO document
# TODO calculate cut network twice here and pass that to concept caching so it
# isn't calulated for each concept. cut passing needs serious refactoring
# anyway actually
def _evaluate_partition(uncut_subsystem, partition,
                        unpartitioned_constellation):
    # Compute forward mip.
    forward_cut = Cut(partition[0], partition[1])
    forward_cut_subsystem = Subsystem(uncut_subsystem.node_indices,
                                      uncut_subsystem.network,
                                      cut=forward_cut,
                                      mice_cache=uncut_subsystem._mice_cache)
    forward_constellation = constellation(forward_cut_subsystem)
    forward_mip = BigMip(
        phi=constellation_distance(unpartitioned_constellation,
                                   forward_constellation,
                                   uncut_subsystem),
        unpartitioned_constellation=unpartitioned_constellation,
        partitioned_constellation=forward_constellation,
        subsystem=uncut_subsystem)
    # Compute backward mip.
    backward_cut = Cut(partition[1], partition[0])
    backward_cut_subsystem = Subsystem(uncut_subsystem.node_indices,
                                       uncut_subsystem.network,
                                       cut=backward_cut,
                                       mice_cache=uncut_subsystem._mice_cache)
    backward_constellation = constellation(backward_cut_subsystem)
    backward_mip = BigMip(
        phi=constellation_distance(unpartitioned_constellation,
                                   backward_constellation,
                                   uncut_subsystem),
        unpartitioned_constellation=unpartitioned_constellation,
        partitioned_constellation=backward_constellation,
        subsystem=uncut_subsystem)
    # Choose minimal unidirectional cut.
    mip = min(forward_mip, backward_mip)
    return mip


# TODO document big_mip
@memory.cache(ignore=["subsystem"])
def _big_mip(cache_key, subsystem):
    # Special case for single-node subsystems.
    if (len(subsystem.nodes) == 1):
        return _single_node_mip(subsystem)

    # Check for degenerate cases
    # =========================================================================
    # Phi is necessarily zero if the subsystem is:
    #   - not strongly connected;
    #   - empty; or
    #   - an elementary mechanism (i.e. no nontrivial bipartitions).
    # So in those cases we immediately return a null MIP.

    if not subsystem:
        return _null_mip(subsystem)

    # Get the connectivity of just the subsystem nodes.
    submatrix_indices = np.ix_([node.index for node in subsystem.nodes],
                               [node.index for node in subsystem.nodes])
    cm = subsystem.network.connectivity_matrix[submatrix_indices]
    # Get the number of strongly connected components.
    num_components, _ = connected_components(csr_matrix(cm))
    if num_components > 1:
        return _null_mip(subsystem)
    # =========================================================================

    # The first bipartition is the null cut (trivial bipartition), so skip it.
    bipartitions = utils.bipartition(subsystem.node_indices)[1:]

    unpartitioned_constellation = constellation(subsystem)
    # Parallel loop over all partitions, using the specified number of cores.
    mip_candidates = Parallel(n_jobs=(constants.NUMBER_OF_CORES),
                              verbose=constants.PARALLEL_VERBOSITY)(
        delayed(_evaluate_partition)(subsystem,
                                     partition,
                                     unpartitioned_constellation)
        for partition in bipartitions)

    return min(mip_candidates)


# Wrapper to ensure that the cache key is the native hash of the subsystem, so
# joblib doesn't mistakenly recompute things when the subsystem's MICE cache is
# changed.
@functools.wraps(_big_mip)
def big_mip(subsystem):
    """Return the MIP of a subsystem.

    Args:
        subsystem (Subsystem): The candidate set of nodes.

    Returns:
        ``BigMip`` -- A nested structure containing all the data from the
        intermediate calculations. The top level contains the basic MIP
        information for the given subsystem. See :class:`models.BigMip`.
    """
    return _big_mip(hash(subsystem), subsystem)


@lru_cache(maxmem=constants.MAXIMUM_CACHE_MEMORY_PERCENTAGE)
def big_phi(subsystem):
    """Return the |big_phi| value of a subsystem."""
    return big_mip(subsystem).phi


@lru_cache(maxmem=constants.MAXIMUM_CACHE_MEMORY_PERCENTAGE)
def main_complex(network):
    """Return the main complex of the network."""
    if not isinstance(network, Network):
        raise ValueError(
            """Input must be a Network (perhaps you passed a Subsystem
            instead?)""")
    return max(complexes(network))


def subsystems(network):
    """Return a generator of all possible subsystems of a network.

    This is the just powerset of the network's set of nodes."""
    for subset in utils.powerset(range(network.size)):
        yield Subsystem(subset, network.current_state, network.past_state,
                        network)


def complexes(network):
    """Return a generator for all complexes of the network.

    This includes reducible, zero-phi complexes (which are not, strictly
    speaking, complexes at all)."""
    if not isinstance(network, Network):
        raise ValueError(
            """Input must be a Network (perhaps you passed a Subsystem
            instead?)""")
    return (big_mip(subsystem) for subsystem in subsystems(network))
