# Part of Odoo. See LICENSE file for full copyright and licensing details.
from lxml import etree
from unittest import mock

from odoo.exceptions import UserError
from odoo.addons.l10n_ar.tests.common import TestAr
from odoo.tests import tagged
from odoo.tools.misc import file_open
from odoo.tools.zeep import Transport
from contextlib import contextmanager
import base64
import logging
import re
from requests import Response

_logger = logging.getLogger(__name__)

@tagged('external_l10n', '-at_install', 'post_install', '-standard', 'external')
class TestEdi(TestAr):

    @staticmethod
    def setup_afip_ws(afip_ws="wsfe"):
        def _decorator(function):
            def wrapper(self):
                self.afip_ws = afip_ws
                function(self)
            return wrapper

        return _decorator

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ar_private_key = cls.env['certificate.key'].create({
            'name': 'AR Test Private key 1',
            'content': base64.b64encode(file_open("l10n_ar_edi/tests/private_key.pem", 'rb').read()),
        })

        cls.ar_certificate_1 = cls.env['certificate.certificate'].create({
            'name': 'AR Test certificate 1',
            'content': base64.b64encode(file_open("l10n_ar_edi/tests/test_cert1.crt", 'rb').read()),
            'private_key_id': cls.ar_private_key.id,
        })

        cls.ar_certificate_2 = cls.env['certificate.certificate'].create({
            'name': 'AR Test certificate 2',
            'content': base64.b64encode(file_open("l10n_ar_edi/tests/test_cert2.crt", 'rb').read()),
            'private_key_id': cls.ar_private_key.id,
        })

        cls.ar_certificate_3 = cls.env['certificate.certificate'].create({
            'name': 'AR Test certificate 3',
            'content': base64.b64encode(file_open("l10n_ar_edi/tests/test_cert3.crt", 'rb').read()),
            'private_key_id': cls.ar_private_key.id,
        })

        cls.company_ri.write({
            'l10n_ar_afip_ws_environment': 'testing',
            'l10n_ar_afip_ws_crt_id': cls.ar_certificate_1,
            'l10n_ar_afip_ws_key_id': cls.ar_private_key.id,
        })
        cls.company_mono.write({
            'l10n_ar_afip_ws_environment': 'testing',
            'l10n_ar_afip_ws_key_id': cls.ar_private_key.id,
        })

        cls._create_afip_connections(cls, cls.company_ri, cls.afip_ws)

    # Initialition
    def _create_afip_connections(self, company, afip_ws):
        """ Method used to create afip connections and commit then to re use this connections in all the test.
        If a connection can not be set because another instance is already using the certificate then we assign a
        random certificate and try again to create the connections. """
        # In order to connect AFIP we need to create a token which depend on the configured AFIP certificate.
        # If the certificate is been used by another testing instance will raise an error telling us that the token
        # can not be used and need to wait 10 minuts or change with another certificate.
        # To avoid this and always run the unit tests we randonly change the certificate and try to create the
        # connection until there is not token error.
        _logger.log(25, 'Setting homologation private key to company %s', company.name)
        company = company.with_context(l10n_ar_invoice_skip_commit=True)
        checked_certificate_token = False

        while not checked_certificate_token:
            try:
                company._l10n_ar_get_connection(afip_ws)
                checked_certificate_token = True
            except Exception as error:
                if 'El CEE ya posee un TA valido para el acceso al WSN solicitado' in repr(error):
                    _logger.log(25, 'Connection Failed')
                elif 'Missing certificate' in repr(error):
                    _logger.log(25, 'Not certificate configured yet')
                else:
                    raise error

                # Set testing certificate
                old = company.l10n_ar_afip_ws_crt_id.name or 'NOT DEFINED'
                current_cert_num = re.findall(r"OdootTestsCert(.)", old)
                current_cert_num = current_cert_num and int(current_cert_num[0]) or 0
                new_cert_number = 1 if current_cert_num == 3 else current_cert_num + 1

                company.l10n_ar_afip_ws_crt_id = self.env['certificate.certificate'].search([('name', '=', 'AR Test certificate %d' % new_cert_number)], limit=1)
                _logger.log(25, 'Setting demo certificate from %s to %s in %s company' % (
                    old, company.l10n_ar_afip_ws_crt_id.name, company.name))

    def _prepare_multicurrency_values(self):
        super()._prepare_multicurrency_values()
        # Set Rates for USD currency takint into account the value from AFIP
        USD = self.env.ref('base.USD')
        _date, value = USD.with_context(l10n_ar_invoice_skip_commit=True)._l10n_ar_get_afip_ws_currency_rate(self.journal.l10n_ar_afip_ws)
        self._set_today_rate(USD, 1.0 / value)

    # Re used unit tests methods

    def _test_connection(self):
        """ Review that the connection is made and all the documents are syncronized"""
        with self.assertRaisesRegex(UserError, '"Check Available AFIP PoS" is not implemented in testing mode for webservice'):
            self.journal.with_context(l10n_ar_invoice_skip_commit=True).l10n_ar_check_afip_pos_number()

    def _test_consult_invoice(self, expected_result=None):
        invoice = self._create_invoice_product()
        self._validate_and_review(invoice, expected_result=expected_result)

        # Consult the info about the last invoice
        last = invoice.journal_id._l10n_ar_get_afip_last_invoice_number(invoice.l10n_latam_document_type_id)
        document_parts = invoice._l10n_ar_get_document_number_parts(invoice.l10n_latam_document_number, invoice.l10n_latam_document_type_id.code)
        self.assertEqual(last, document_parts['invoice_number'])

        # Consult the info about specific invoice
        with self.assertRaisesRegex(UserError, '(CodAutorizacion|Cae).*%s' % invoice.l10n_ar_afip_auth_code):
            self.env['l10n_ar_afip.ws.consult'].create({'number': last,
                                                        'journal_id': invoice.journal_id.id,
                                                        'document_type_id': invoice.l10n_latam_document_type_id.id}).button_confirm()
        return invoice

    def _test_case(self, document_type, concept, forced_values=None, expected_document=None, expected_result=None):
        values = {}
        forced_values = forced_values or {}
        create_invoice = {'product': self._create_invoice_product,
                          'service': self._create_invoice_service,
                          'product_service': self._create_invoice_product_service}
        create_invoice = create_invoice.get(concept)
        expected_document = self.document_type[document_type]

        if 'mipyme' in document_type:
            values.update({'document_type': expected_document, 'lines': [{'price_unit': 150000}]})
            # We need to define the default value for Optional 27 - Transmission Type
            self.env.company.l10n_ar_fce_transmission_type = 'SCA'

            if '_a' in document_type or '_c' in document_type:
                values.update({'partner': self.partner_mipyme})
            elif '_b' in document_type:
                values.update({'partner': self.partner_mipyme_ex})
        elif '_b' in document_type:
            values.update({'partner': self.partner_cf})

        values.update(forced_values)
        invoice = create_invoice(values)
        self.assertEqual(invoice.l10n_latam_document_type_id.display_name, expected_document.display_name, 'The document should be %s' % expected_document.display_name)
        self._validate_and_review(invoice, expected_result=expected_result)
        return invoice

    def _test_case_credit_note(self, document_type, invoice, data=None, expected_result=None):
        refund = self._create_credit_note(invoice, data=data)
        expected_document = self.document_type[document_type]
        self.assertEqual(refund.l10n_latam_document_type_id.display_name, expected_document.display_name, 'The document should be %s' % expected_document.display_name)
        self._validate_and_review(refund, expected_result=expected_result)
        return refund

    def _test_case_debit_note(self, document_type, invoice, data=None, expected_result='A'):
        debit_note = self._create_debit_note(invoice, data=data)
        expected_document = self.document_type[document_type]
        self.assertEqual(debit_note.l10n_latam_document_type_id.display_name, expected_document.display_name, 'The document should be %s' % expected_document.display_name)
        self._validate_and_review(debit_note, expected_result=expected_result)
        return debit_note

    # Helpers

    @classmethod
    def _get_afip_pos_system_real_name(cls):
        mapping = super()._get_afip_pos_system_real_name()
        mapping.update({'WSFE': 'RAW_MAW', 'WSFEX': 'FEEWS', 'WSBFE': 'BFEWS'})
        return mapping

    @contextmanager
    def _handler_afip_internal_error(self):
        try:
            yield
        except Exception as exc:
            error_msg = repr(exc)
            if 'Code 500' in error_msg or 'Code 501' in error_msg or 'Code 502' in error_msg:
                self.skipTest("We receive an internal error from AFIP so skip this test")
            else:
                raise

    def _post(self, invoice):
        with self._handler_afip_internal_error():
            invoice.with_context(l10n_ar_invoice_skip_commit=True).action_post()

    def _l10n_ar_verify_on_afip(self, bill):
        with self._handler_afip_internal_error():
            bill.l10n_ar_verify_on_afip()

    def _validate_and_review(self, invoice, expected_result=None, error_msg=None):
        """ Validate electronic invoice and review that the invoice has been proper validated """

        expected_result = expected_result or 'A'
        error_msg = error_msg or 'This test return a result different from the expteced (%s)' % expected_result
        self._post(invoice)

        # EDI validations
        self.assertEqual(invoice.l10n_ar_afip_auth_mode, 'CAE', error_msg)
        detail_info = error_msg + '\nReponse\n' + invoice.l10n_ar_afip_xml_response + '\nMsg\n' + invoice.message_ids[0].body
        self.assertEqual(invoice.l10n_ar_afip_result, expected_result, detail_info)

        self.assertTrue(invoice.l10n_ar_afip_auth_code, error_msg)
        self.assertTrue(invoice.l10n_ar_afip_auth_code_due, error_msg)
        self.assertTrue(invoice.l10n_ar_afip_xml_request, error_msg)
        self.assertTrue(invoice.l10n_ar_afip_xml_response, error_msg)

    def _l10n_ar_xml_tag(self, afip_ws, data):
        """ Easy helper to get XML tag for a given purpose data """
        xml_tags = {
            'wsfe': {'currency': 'MonId', 'rate': 'MonCotiz'},
            'wsfex': {'currency': 'Moneda_Id', 'rate': 'Moneda_ctz'},
            'wsbfe': {'currency': 'Imp_moneda_Id', 'rate': 'Imp_moneda_ctz'}}
        return xml_tags[afip_ws][data]

    def _test_payment_foreign_currency(self):
        """ Payment in Foreign Currency  """
        USD = self.env.ref('base.USD')
        self.assertEqual(USD.rate, 1.0)
        self._prepare_multicurrency_values()
        self.assertNotEqual(USD.rate, 1.0)
        afip_ws = self.journal.l10n_ar_afip_ws

        # No + any rate (does not matter rate): Will work always is the current behavior, is the default value
        invoice = self._create_invoice({"currency": USD})
        self.assertEqual(invoice.l10n_ar_payment_foreign_currency, "No")
        self._validate_and_review(invoice)
        currency_tag = self._l10n_ar_xml_tag(afip_ws, 'currency')
        self.assertIn(f"<ns0:{currency_tag}>DOL</ns0:{currency_tag}>", invoice.l10n_ar_afip_xml_request)
        self.assertIn("<ns0:CanMisMonExt>N</ns0:CanMisMonExt>", invoice.l10n_ar_afip_xml_request)

        # Yes + Correct last business day rate: Will work
        self.env['ir.config_parameter'].sudo().set_param(
            f"l10n_ar_edi.{self.env.company.id}_foreign_currency_payment", "Yes")
        invoice = self._create_invoice({"currency": USD})
        self.assertEqual(invoice.l10n_ar_payment_foreign_currency, "Yes")
        self._validate_and_review(invoice)
        self.assertIn(f"<ns0:{currency_tag}>DOL</ns0:{currency_tag}>", invoice.l10n_ar_afip_xml_request)
        self.assertIn("<ns0:CanMisMonExt>S</ns0:CanMisMonExt>", invoice.l10n_ar_afip_xml_request)

        # Yes + bad rate: Will fail because is not last business day
        USD.rate_ids.rate = USD.rate_ids.rate * 0.10
        invoice = self._create_invoice({"currency": USD})
        with self.assertRaisesRegex(UserError, "The rate to be reported.*differs from that of ARCA Remember that if you pay in foreign currency you must use the same rate of the last business day of ARCA"):
            self._validate_and_review(invoice)

        # Yes + No rate defined: Will raise error
        USD.rate_ids.rate = 1.0
        invoice = self._create_invoice({"currency": USD})
        with self.assertRaisesRegex(UserError, "The currency rate to be reported.*is not valid. It must be between"):
            self._validate_and_review(invoice)


class TestFexCommon(TestEdi):

    @classmethod
    def setUpClass(cls):
        super(TestFexCommon, cls).setUpClass('wsfex')

        cls.partner = cls.res_partner_barcelona_food
        cls.incoterm = cls.env.ref('account.incoterm_EXW')
        cls.journal = cls._create_journal(cls, 'wsfex')

        # Document Types
        cls.document_type.update({
            'invoice_e': cls.env.ref('l10n_ar.dc_e_f'),
            'credit_note_e': cls.env.ref('l10n_ar.dc_e_nc')})

    def _create_invoice(self, data=None, invoice_type='out_invoice'):
        data = data or {}
        data.update({'incoterm': self.incoterm})
        return super()._create_invoice(data=data, invoice_type=invoice_type)

    def _create_invoice_product(self, data=None):
        data = data or {}
        data.update({'incoterm': self.incoterm})
        return super()._create_invoice_product(data=data)

    def _create_invoice_product_service(self, data=None):
        data = data or {}
        data.update({'incoterm': self.incoterm})
        return super()._create_invoice_product_service(data=data)


class TestArEdiMockedCommon(TestEdi):
    @contextmanager
    def patch_client(self, responses):
        """ Patch zeep.Transport in l10n_ar_edi/models/l10n_ar_afipws_connection.py"""

        self.maxDiff = None
        # This method can be called from within a @classmethod, instantiate a TestCase when that happens, so we can use test_case.assert*
        test_case = self if hasattr(self.assertEqual, '__self__') else self()

        responses = iter(responses)

        class MockedTransport(Transport):
            def _load_remote_data(self, url):
                """ Before we make any interactions with the server, we first need to get the
                schema of datatypes so we know what services are available. There are only two
                URLS for Argentina (LoginCms, and services.asmx?WSDL) we are testing so we can go
                view these files directly.
                """
                service = url.rpartition("/")[2].partition("?")[0]
                module = 'l10n_ar_edi'
                with file_open(f'{module}/tests/expected_requests/{service}-schema.xml', 'rb') as fd:
                    expected_tree = fd.read()
                return expected_tree

            def post(self, address, message, headers):
                expected_service, expected_request_filename, response_filename = next(responses)
                if 'service.asmx' in address:
                    _, _, service = headers.get('SOAPAction').rpartition("/")
                    service = service[:-1]
                else:
                    _, _, service = address.rpartition("/")

                test_case.assertEqual(service, expected_service)

                module = 'l10n_ar_edi'
                with file_open(f'{module}/tests/expected_requests/{expected_request_filename}.xml', 'rb') as fd:
                    expected_tree = etree.fromstring(fd.read())

                request_tree = etree.fromstring(message)
                try:
                    test_case.assertXmlTreeEqual(request_tree, expected_tree)
                except AssertionError:
                    _logger.error('Unexpected request XML for service %s', service)
                    raise

                with file_open(f'{module}/tests/mocked_responses/{response_filename}.xml', 'rb') as fd:
                    response_content = fd.read()

                response = mock.Mock(spec=Response)
                response.status_code = 200
                response.content = response_content
                response.headers = {'Content-Type': 'text/xml;charset=utf-8'}
                self.xml_request = etree.tostring(
                    request_tree, pretty_print=True).decode('utf-8')
                self.xml_response = etree.tostring(
                    etree.fromstring(response_content), pretty_print=True).decode('utf-8')
                return response

        with mock.patch('odoo.addons.l10n_ar_edi.models.l10n_ar_afipws_connection.ARTransport', new=MockedTransport):
            yield

        if next(responses, None):
            test_case.fail('Not all expected calls were made!')

    def _create_afip_connections(self, company, afip_ws):
        # Override to mock the connection instead of actually making the network requests.
        # No need to call super as it is mainly just running the same code in a loop.
        company = company.with_context(l10n_ar_invoice_skip_commit=True)
        with self.patch_client(self, [('LoginCms', 'LoginCms-final', 'LoginCms-final')]):
            company._l10n_ar_get_connection(afip_ws)
