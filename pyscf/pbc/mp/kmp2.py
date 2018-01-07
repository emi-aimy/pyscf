#!/usr/bin/env python
# $Id$
# -*- coding: utf-8

import time
from functools import reduce
import numpy
import numpy as np

from pyscf import lib
from pyscf.lib import logger

import pyscf.pbc.tools.pbc as tools
from pyscf.pbc.cc.kccsd import get_moidx
from pyscf.pbc.cc.kccsd_rhf import get_nocc, get_nmo
from pyscf.pbc.cc.kpoint_helper import unique_pqr_list

'''
kpoint-adapted and spin-adapted MP2
t2[i,j,a,b] = <ij|ab> / D_ij^ab
'''

def kernel(mp, mo_energy, mo_coeff, eris=None, verbose=logger.NOTE):
    if eris is None:
        eris = mp.ao2mo(mo_coeff)

    nocc = mp.nocc
    nvir = mp.nmo - nocc
    nkpts = mp.nkpts
    t2 = numpy.zeros((nkpts,nkpts,nkpts,nocc,nocc,nvir,nvir), dtype=eris.fock.dtype)

    woovv = numpy.empty((nkpts,nkpts,nkpts,nocc,nocc,nvir,nvir), dtype=eris.fock.dtype)
    emp2 = 0
    foo = eris.fock[:,:nocc,:nocc].copy()
    fvv = eris.fock[:,nocc:,nocc:].copy()
    eia = numpy.zeros((nocc,nvir))
    eijab = numpy.zeros((nocc,nocc,nvir,nvir))

    kconserv = mp.kconserv
    for ki in range(nkpts):
      for kj in range(nkpts):
        for ka in range(nkpts):
            kb = kconserv[ki,ka,kj]
            eia = np.diagonal(foo[ki]).reshape(-1,1) - np.diagonal(fvv[ka])
            ejb = np.diagonal(foo[kj]).reshape(-1,1) - np.diagonal(fvv[kb])
            eijab = lib.direct_sum('ia,jb->ijab',eia,ejb)
            woovv[ki,kj,ka] = (2*eris.oovv[ki,kj,ka] - eris.oovv[ki,kj,kb].transpose(0,1,3,2))
            t2[ki,kj,ka] = eris.oovv[ki,kj,ka] / eijab

    t2 = numpy.conj(t2)
    emp2 = numpy.einsum('pqrijab,pqrijab',t2,woovv).real
    emp2 /= nkpts

    return emp2, t2


class KMP2(lib.StreamObject):
    def __init__(self, mf, frozen=0, mo_coeff=None, mo_occ=None):

        if mo_coeff  is None: mo_coeff  = mf.mo_coeff
        if mo_occ    is None: mo_occ    = mf.mo_occ

        self.mol = mf.mol
        self._scf = mf
        self.verbose = self.mol.verbose
        self.stdout = self.mol.stdout
        self.max_memory = mf.max_memory

        self.frozen = frozen

##################################################
# don't modify the following attributes, they are not input options
        self.kpts = mf.kpts
        self.mo_energy = mf.mo_energy
        self.nkpts = len(self.kpts)
        self.kconserv = tools.get_kconserv(mf.cell, mf.kpts)
        self.khelper = unique_pqr_list(mf.cell, mf.kpts)
        self.mo_energy = mf.mo_energy
        self.mo_coeff = mo_coeff
        self.mo_occ = mo_occ
        self._nocc = None
        self._nmo = None
        self.emp2 = None
        self.e_corr = None
        self.t2 = None
        self._keys = set(self.__dict__.keys())

    nocc = property(get_nocc)
    @nocc.setter
    def nocc(self, n):
        self._nocc = n

    nmo = property(get_nmo)
    @nmo.setter
    def nmo(self, n):
        self._nmo = n

    get_nocc = get_nocc
    get_nmo = get_nmo

    def kernel(self, mo_energy=None, mo_coeff=None, eris=None):
        if mo_energy is None:
            mo_energy = self.mo_energy
        if mo_coeff is None:
            mo_coeff = self.mo_coeff
        if mo_energy is None or mo_coeff is None:
            log = logger.Logger(self.stdout, self.verbose)
            log.warn('mo_coeff, mo_energy are not given.\n'
                     'You may need to call mf.kernel() to generate them.')
            raise RuntimeError

        self.emp2, self.t2 = \
                kernel(self, mo_energy, mo_coeff, eris, verbose=self.verbose)
        logger.log(self, 'KMP2 energy = %.15g', self.emp2)
        self.e_corr = self.emp2
        return self.emp2, self.t2

    def ao2mo(self, mo_coeff=None):
        return _ERIS(self, mo_coeff, verbose=self.verbose)


def _mem_usage(nkpts, nocc, nvir):
    incore = nkpts**3*(nocc+nvir)**4
    # Roughly, factor of two for intermediates and factor of two
    # for safety (temp arrays, copying, etc)
    incore *= 4
    # TODO: Improve incore estimate and add outcore estimate
    outcore = basic = incore
    return incore*16/1e6, outcore*16/1e6, basic*16/1e6

class _ERIS:
    def __init__(self, mp, mo_coeff=None, verbose=None):
        cput0 = (time.clock(), time.time())
        moidx = get_moidx(mp)
        nkpts = mp.nkpts
        nmo = mp.nmo

        nao = mp.mo_coeff[0].shape[0]
        dtype = mp.mo_coeff[0].dtype
        self.mo_coeff = numpy.zeros((nkpts,nao,nmo), dtype=dtype)
        self.fock = numpy.zeros((nkpts,nmo,nmo), dtype=dtype)
        if mo_coeff is None:
            for kp in range(nkpts):
                self.mo_coeff[kp] = mp.mo_coeff[kp][:,moidx[kp]]
            mo_coeff = self.mo_coeff
            for kp in range(nkpts):
                self.fock[kp] = numpy.diag(mp.mo_energy[kp][moidx[kp]]).astype(dtype)
        else:  # If mo_coeff is not canonical orbital
            for kp in range(nkpts):
                self.mo_coeff[kp] = mo_coeff[kp][:,moidx[kp]]
            mo_coeff = self.mo_coeff
            dm = mp._scf.make_rdm1(mp.mo_coeff, mp.mo_occ)
            # Don't use get_veff(), because mp._scf might be DFT,
            # but veff should be Fock, not Kohn-Sham.
            #fockao = mp._scf.get_hcore() + mp._scf.get_veff(mp.mol, dm)
            vj, vk = mp._scf.get_jk(mp.mol, dm)
            veff = vj - vk * .5
            fockao = mp._scf.get_hcore() + veff
            for kp in range(nkpts):
                self.fock[kp] = reduce(numpy.dot, (mo_coeff[kp].T.conj(), fockao[kp], mo_coeff[kp])).astype(dtype)

        nocc = mp.nocc
        nmo = mp.nmo
        nvir = nmo - nocc
        mem_incore, mem_outcore, mem_basic = _mem_usage(nkpts, nocc, nvir)
        mem_now = lib.current_memory()[0]
        fao2mo = mp._scf.with_df.ao2mo

        kconserv = mp.kconserv
        khelper = mp.khelper
        unique_klist = khelper.get_uniqueList()
        nUnique_klist = khelper.nUnique

        max_memory = max(2000, mp.max_memory*.9-mem_now)
        log = logger.Logger(mp.stdout, mp.verbose)
        if mp.max_memory < mem_basic:
            log.warn('Not enough memory for integral transformation. '
                     'Available mem %s MB, required mem %s MB',
                     max_memory, mem_basic)

        if (mp.mol.incore_anyway or
                (mem_incore+mem_now < mp.max_memory)):
            log.debug('transform (ia|jb) incore')
            eri = numpy.zeros((nkpts,nkpts,nkpts,nmo,nmo,nmo,nmo), dtype=dtype)

            # Looping over unique list of k-vectors
            for pqr in range(nUnique_klist):
                kp, kq, kr = unique_klist[pqr]
                ks = kconserv[kp,kq,kr]
                eri_kpt = fao2mo((mo_coeff[kp],mo_coeff[kq],mo_coeff[kr],mo_coeff[ks]),
                                 (mp.kpts[kp],mp.kpts[kq],mp.kpts[kr],mp.kpts[ks]), compact=False)
                eri_kpt = eri_kpt.reshape(nmo,nmo,nmo,nmo)
                eri[kp,kq,kr] = eri_kpt.copy()

            for kp in range(nkpts):
                for kq in range(nkpts):
                    for kr in range(nkpts):
                        ikp, ikq, ikr = khelper.get_irrVec(kp,kq,kr)
                        irr_eri = eri[ikp,ikq,ikr]
                        eri[kp,kq,kr] = khelper.transform_irr2full(irr_eri,kp,kq,kr)

            # <ij|kl> = (ik|jl)
            eri = eri.transpose(0,2,1,3,5,4,6)

            self.dtype = eri.dtype
            self.oovv = eri[:,:,:,:nocc,:nocc,nocc:,nocc:].copy() / nkpts

        log.timer('Integral transformation', *cput0)


if __name__ == '__main__':
    from pyscf.pbc import gto, scf, mp 

    cell = gto.Cell()
    cell.atom='''
    C 0.000000000000   0.000000000000   0.000000000000
    C 1.685068664391   1.685068664391   1.685068664391
    '''
    cell.basis = 'gth-szv'
    cell.pseudo = 'gth-pade'
    cell.a = '''
    0.000000000, 3.370137329, 3.370137329
    3.370137329, 0.000000000, 3.370137329
    3.370137329, 3.370137329, 0.000000000'''
    cell.unit = 'B'
    cell.verbose = 5
    cell.build()

    # Running HF and MP2 with 1x1x2 Monkhorst-Pack k-point mesh
    kmf = scf.KRHF(cell, kpts=cell.make_kpts([1,1,2]), exxdiv=None)
    ehf = kmf.kernel()

    mymp = mp.KMP2(kmf)
    emp2, t2 = mymp.kernel()
    print(emp2 - -0.204721432828996)

