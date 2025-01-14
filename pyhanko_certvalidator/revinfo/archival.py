import abc
import enum
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, List, Optional, TypeVar, Union

from asn1crypto import algos, crl, ocsp

from pyhanko_certvalidator._types import type_name
from pyhanko_certvalidator.ltv.types import (
    IssuedItemContainer,
    ValidationTimingParams,
)
from pyhanko_certvalidator.policy_decl import (
    CertRevTrustPolicy,
    FreshnessReqType,
)


class RevinfoUsabilityRating(enum.Enum):
    OK = enum.auto()
    STALE = enum.auto()
    TOO_NEW = enum.auto()
    UNCLEAR = enum.auto()

    @property
    def usable(self) -> bool:
        return self == RevinfoUsabilityRating.OK


class RevinfoContainer(IssuedItemContainer, abc.ABC):
    def usable_at(
        self, policy: CertRevTrustPolicy, timing_params: ValidationTimingParams
    ) -> RevinfoUsabilityRating:
        raise NotImplementedError

    @property
    def revinfo_sig_mechanism_used(
        self,
    ) -> Optional[algos.SignedDigestAlgorithm]:
        raise NotImplementedError


RevInfoType = TypeVar('RevInfoType', bound=RevinfoContainer)


def sort_freshest_first(lst: Iterable[RevInfoType]) -> List[RevInfoType]:
    def _key(container: RevinfoContainer):
        dt = container.issuance_date
        # if dt is None ---> (0, None)
        # else ---> (1, dt)
        # This ensures that None is never compared to anything (which would
        #  cause a TypeError), and that (0, None) gets sorted before everything
        #  else. Since we sort reversed, the "unknown issuance date" ones
        #  are dumped at the end of the list.
        return dt is not None, dt

    return sorted(lst, key=_key, reverse=True)


def _freshness_delta(policy, this_update, next_update, time_tolerance):

    freshness_delta = policy.freshness
    if freshness_delta is None:
        if next_update is not None and next_update >= this_update:
            freshness_delta = next_update - this_update
    if freshness_delta is not None:
        freshness_delta = abs(freshness_delta) + time_tolerance
    return freshness_delta


def _judge_revinfo(
    this_update: Optional[datetime],
    next_update: Optional[datetime],
    policy: CertRevTrustPolicy,
    timing_params: ValidationTimingParams,
) -> RevinfoUsabilityRating:

    if this_update is None:
        return RevinfoUsabilityRating.UNCLEAR

    # Revinfo issued after the validation time doesn't make any sense
    # to consider, except in the case of the (legacy) default policy
    # with retroactive_revinfo active.
    # AdES-derived policies are supposed to use proper POE handling in lieu
    # of this alternative system.
    #  TODO revisit this when we actually implement AdES point-in-time
    #   validation. Maybe this needs to be dealt with at a higher level, to
    #   accept future revinfo as evidence of non-revocation or somesuch
    if timing_params.validation_time < this_update:
        if (
            not policy.retroactive_revinfo
            or policy.freshness_req_type != FreshnessReqType.DEFAULT
        ):
            return RevinfoUsabilityRating.TOO_NEW

    validation_time = timing_params.validation_time
    time_tolerance = timing_params.time_tolerance
    # see 5.2.5.4 in ETSI EN 319 102-1
    if policy.freshness_req_type == FreshnessReqType.TIME_AFTER_SIGNATURE:
        # check whether the revinfo was generated sufficiently long _after_
        # the (presumptive) signature time
        freshness_delta = _freshness_delta(
            policy, this_update, next_update, time_tolerance
        )
        if freshness_delta is None:
            return RevinfoUsabilityRating.UNCLEAR
        signature_poe_time = timing_params.best_signature_time
        if this_update - signature_poe_time < freshness_delta:
            return RevinfoUsabilityRating.STALE
    elif (
        policy.freshness_req_type
        == FreshnessReqType.MAX_DIFF_REVOCATION_VALIDATION
    ):
        # check whether the difference between thisUpdate
        # and the validation time is small enough

        # add time_tolerance to allow for additional time drift
        freshness_delta = _freshness_delta(
            policy, this_update, next_update, time_tolerance
        )
        if freshness_delta is None:
            return RevinfoUsabilityRating.UNCLEAR

        # See ETSI EN 319 102-1, § 5.2.5.4, item 2)
        #  in particular, "too recent" doesn't seem to apply;
        #  the result is pass/fail
        if this_update < validation_time - freshness_delta:
            return RevinfoUsabilityRating.STALE
    elif policy.freshness_req_type == FreshnessReqType.DEFAULT:
        # check whether the validation time falls within the
        # thisUpdate-nextUpdate window (non-AdES!!)
        if next_update is None:
            return RevinfoUsabilityRating.UNCLEAR

        retroactive = policy.retroactive_revinfo

        if not retroactive and validation_time < this_update - time_tolerance:
            return RevinfoUsabilityRating.TOO_NEW
        if validation_time > next_update + time_tolerance:
            return RevinfoUsabilityRating.STALE
    else:  # pragma: nocover
        raise NotImplementedError
    return RevinfoUsabilityRating.OK


def _extract_basic_ocsp_response(
    ocsp_response,
) -> Optional[ocsp.BasicOCSPResponse]:

    # Make sure that we get a valid response back from the OCSP responder
    status = ocsp_response['response_status'].native
    if status != 'successful':
        return None

    response_bytes = ocsp_response['response_bytes']
    if response_bytes['response_type'].native != 'basic_ocsp_response':
        return None

    return response_bytes['response'].parsed


@dataclass(frozen=True)
class OCSPContainer(RevinfoContainer):
    ocsp_response_data: ocsp.OCSPResponse
    index: int = 0

    @classmethod
    def load_multi(
        cls, ocsp_response: ocsp.OCSPResponse
    ) -> List['OCSPContainer']:
        basic_ocsp_response = _extract_basic_ocsp_response(ocsp_response)
        if basic_ocsp_response is None:
            return []
        tbs_response = basic_ocsp_response['tbs_response_data']

        return [
            OCSPContainer(ocsp_response_data=ocsp_response, index=ix)
            for ix in range(len(tbs_response['responses']))
        ]

    @property
    def issuance_date(self) -> Optional[datetime]:
        cert_response = self.extract_single_response()
        if cert_response is None:
            return None

        return cert_response['this_update'].native

    def usable_at(
        self, policy: CertRevTrustPolicy, timing_params: ValidationTimingParams
    ) -> RevinfoUsabilityRating:

        cert_response = self.extract_single_response()
        if cert_response is None:
            return RevinfoUsabilityRating.UNCLEAR

        this_update = cert_response['this_update'].native
        next_update = cert_response['next_update'].native
        return _judge_revinfo(
            this_update,
            next_update,
            policy=policy,
            timing_params=timing_params,
        )

    def extract_basic_ocsp_response(self) -> Optional[ocsp.BasicOCSPResponse]:
        return _extract_basic_ocsp_response(self.ocsp_response_data)

    def extract_single_response(self) -> Optional[ocsp.SingleResponse]:
        basic_ocsp_response = self.extract_basic_ocsp_response()
        if basic_ocsp_response is None:
            return None
        tbs_response = basic_ocsp_response['tbs_response_data']

        if len(tbs_response['responses']) <= self.index:
            return None
        return tbs_response['responses'][self.index]

    @property
    def revinfo_sig_mechanism_used(
        self,
    ) -> Optional[algos.SignedDigestAlgorithm]:
        basic_resp = self.extract_basic_ocsp_response()
        return None if basic_resp is None else basic_resp['signature_algorithm']


@dataclass(frozen=True)
class CRLContainer(RevinfoContainer):
    crl_data: crl.CertificateList

    def usable_at(
        self, policy: CertRevTrustPolicy, timing_params: ValidationTimingParams
    ) -> RevinfoUsabilityRating:
        tbs_cert_list = self.crl_data['tbs_cert_list']
        this_update = tbs_cert_list['this_update'].native
        next_update = tbs_cert_list['next_update'].native
        return _judge_revinfo(
            this_update, next_update, policy=policy, timing_params=timing_params
        )

    @property
    def issuance_date(self) -> Optional[datetime]:
        tbs_cert_list = self.crl_data['tbs_cert_list']
        return tbs_cert_list['this_update'].native

    @property
    def revinfo_sig_mechanism_used(self) -> algos.SignedDigestAlgorithm:
        return self.crl_data['signature_algorithm']


LegacyCompatCRL = Union[bytes, crl.CertificateList, CRLContainer]
LegacyCompatOCSP = Union[bytes, ocsp.OCSPResponse, OCSPContainer]


def process_legacy_crl_input(
    crls: Iterable[LegacyCompatCRL],
) -> List[CRLContainer]:
    new_crls = []
    for crl_ in crls:
        if isinstance(crl_, bytes):
            crl_ = crl.CertificateList.load(crl_)
        if isinstance(crl_, crl.CertificateList):
            crl_ = CRLContainer(crl_)
        if isinstance(crl_, CRLContainer):
            new_crls.append(crl_)
        else:
            raise TypeError(
                f"crls must be a list of byte strings or "
                f"asn1crypto.crl.CertificateList objects, not {type_name(crl_)}"
            )
    return new_crls


def process_legacy_ocsp_input(
    ocsps: Iterable[LegacyCompatOCSP],
) -> List[OCSPContainer]:
    new_ocsps = []
    for ocsp_ in ocsps:
        if isinstance(ocsp_, bytes):
            ocsp_ = ocsp.OCSPResponse.load(ocsp_)
        if isinstance(ocsp_, ocsp.OCSPResponse):
            extr = OCSPContainer.load_multi(ocsp_)
            new_ocsps.extend(extr)
        elif isinstance(ocsp_, OCSPContainer):
            new_ocsps.append(ocsp_)
        else:
            raise TypeError(
                f"ocsps must be a list of byte strings or "
                f"asn1crypto.ocsp.OCSPResponse objects, not {type_name(ocsp_)}"
            )
    return new_ocsps
