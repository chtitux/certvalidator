from typing import Union, Iterable

import logging
import aiohttp

from asn1crypto import ocsp, x509, cms

from ... import errors
from ..api import OCSPFetcher
from .util import AIOHttpMixin, LazySession
from ..common_utils import (
    process_ocsp_response_data, format_ocsp_request, ocsp_job_get_earliest
)
from ...util import issuer_serial

logger = logging.getLogger(__name__)


class AIOHttpOCSPFetcher(OCSPFetcher, AIOHttpMixin):

    def __init__(self, session: Union[aiohttp.ClientSession, LazySession],
                 user_agent=None, per_request_timeout=10,
                 certid_hash_algo='sha1', request_nonces=True):
        super().__init__(session, user_agent, per_request_timeout)
        if certid_hash_algo not in ('sha1', 'sha256'):
            raise ValueError(
                f'certid_hash_algo must be one of "sha1", "sha256", not '
                f'{repr(certid_hash_algo)}'
            )
        self.certid_hash_algo = certid_hash_algo
        self.request_nonces = request_nonces

    async def fetch(self,
                    cert: Union[x509.Certificate, cms.AttributeCertificateV2],
                    issuer: x509.Certificate) -> ocsp.OCSPResponse:

        tag = (issuer_serial(cert), issuer.issuer_serial)
        logger.info(
            f"About to queue OCSP fetch for {cert.subject.human_friendly}..."
        )

        async def task():
            return await self._fetch(cert, issuer)

        return await self._post_fetch_task(tag, task)

    async def _fetch(self,
                     cert: Union[x509.Certificate, cms.AttributeCertificateV2],
                     issuer: x509.Certificate):
        ocsp_request = format_ocsp_request(
            cert, issuer, certid_hash_algo=self.certid_hash_algo,
            request_nonces=self.request_nonces
        )
        # Try the OCSP responders in arbitrary order, and process the responses
        # as they come in
        ocsp_urls = cert.ocsp_urls
        if not ocsp_urls:
            raise errors.OCSPFetchError("No URLs to fetch OCSP responses from")
        logger.info(
            f"Fetching OCSP status for {cert.subject.human_friendly} from "
            f"url(s) {';'.join(ocsp_urls)}..."
        )
        session = await self.get_session()
        fetch_jobs = (
            _grab_ocsp(
                ocsp_request, ocsp_url, user_agent=self.user_agent,
                session=session, timeout=self.per_request_timeout,
            ) for ocsp_url in ocsp_urls
        )
        return await ocsp_job_get_earliest(fetch_jobs)

    def fetched_responses(self) -> Iterable[ocsp.OCSPResponse]:
        return self.get_results()

    def fetched_responses_for_cert(self, cert: x509.Certificate) \
            -> Iterable[ocsp.OCSPResponse]:
        target_is = issuer_serial(cert)
        return {
            resp for (subj_is, _), resp in self._iter_results()
            if subj_is == target_is
        }


async def _grab_ocsp(ocsp_request: ocsp.OCSPRequest, ocsp_url: str,
                     *, user_agent, session: aiohttp.ClientSession, timeout):
    try:
        logger.info(f"Requesting OCSP response from {ocsp_url}...")
        headers = {
            'Accept': 'application/ocsp-response',
            'Content-Type': 'application/ocsp-request',
            'User-Agent': user_agent
        }
        cl_timeout = aiohttp.ClientTimeout(total=timeout)
        async with session.post(url=ocsp_url, headers=headers,
                                data=ocsp_request.dump(),
                                raise_for_status=True,
                                timeout=cl_timeout) as response:
            response_data = await response.read()
        return process_ocsp_response_data(
            response_data, ocsp_request=ocsp_request,
            ocsp_url=ocsp_url
        )
    except (aiohttp.ClientError, errors.OCSPValidationError) as e:
        raise errors.OCSPFetchError(
            f"Failed to fetch OCSP response from {ocsp_url}",
        ) from e
