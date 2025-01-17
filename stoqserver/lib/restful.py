# -*- coding: utf-8 -*-
# vi:si:et:sw=4:sts=4:ts=4

#
# Copyright (C) 2018 Async Open Source <http://www.async.com.br>
# All rights reserved
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., or visit: http://www.gnu.org/.
#
# Author(s): Stoq Team <stoq-devel@async.com.br>
#

import base64
import datetime
import decimal
import functools
import json
import logging
import os
import psycopg2
from gevent.queue import Queue
from gevent.event import Event
from gevent.lock import Semaphore
import io
import platform
import re
import select
import subprocess
import requests
from hashlib import md5

import gevent
from blinker import signal
import psutil
import tzlocal

from kiwi.component import provide_utility
from kiwi.currency import currency
from flask import request, abort, send_file, make_response, Response, jsonify
from flask_restful import Resource
from serial.serialutil import SerialException
from stoq import version as stoq_version
from stoqdrivers import __version__ as stoqdrivers_version
from stoqdrivers.exceptions import InvalidReplyException

from stoqlib.api import api
from stoqlib.database.runtime import get_current_station
from stoqlib.database.interfaces import ICurrentUser
from stoqlib.database.settings import get_database_version
from stoqlib.domain.events import SaleConfirmedRemoteEvent
from stoqlib.domain.devices import DeviceSettings
from stoqlib.domain.image import Image
from stoqlib.domain.overrides import ProductBranchOverride, SellableBranchOverride
from stoqlib.domain.payment.group import PaymentGroup
from stoqlib.domain.payment.method import PaymentMethod
from stoqlib.domain.payment.card import CreditCardData, CreditProvider, CardPaymentDevice
from stoqlib.domain.payment.payment import Payment
from stoqlib.domain.person import LoginUser, Person, Client, ClientCategory, Individual
from stoqlib.domain.product import Product
from stoqlib.domain.purchase import PurchaseOrder
from stoqlib.domain.sale import Sale
from stoqlib.domain.station import BranchStation
from stoqlib.domain.token import AccessToken
from stoqlib.domain.payment.renegotiation import PaymentRenegotiation
from stoqlib.domain.sellable import (Sellable, SellableCategory,
                                     ClientCategoryPrice)
from stoqlib.domain.till import Till, TillSummary
from stoqlib.exceptions import LoginError, TillError
from stoqlib.lib.configparser import get_config
from stoqlib.lib.dateutils import INTERVALTYPE_MONTH, create_date_interval, localnow
from stoqlib.lib.environment import is_developer_mode
from stoqlib.lib.formatters import raw_document
from stoqlib.lib.translation import dgettext
from stoqlib.lib.pluginmanager import get_plugin_manager, PluginError
from storm.expr import Desc, LeftJoin, Join, And, Eq, Ne, Coalesce

from stoqserver import __version__ as stoqserver_version
from .lock import lock_pinpad, lock_sat, LockFailedException
from .constants import PROVIDER_MAP
from ..api.decorators import login_required, store_provider
from ..signals import (CheckPinpadStatusEvent, CheckSatStatusEvent,
                       GenerateAdvancePaymentReceiptPictureEvent, GenerateInvoicePictureEvent,
                       GrantLoyaltyPointsEvent, PrintAdvancePaymentReceiptEvent,
                       PrintKitchenCouponEvent, ProcessExternalOrderEvent,
                       TefCheckPendingEvent, TefPrintReceiptsEvent)
from ..utils import JsonEncoder


# This needs to be imported to workaround a storm limitation
PurchaseOrder, PaymentRenegotiation

_ = functools.partial(dgettext, 'stoqserver')
PDV_VERSION = None

try:
    from stoqnfe.events import NfeProgressEvent, NfeWarning, NfeSuccess
    from stoqnfe.exceptions import PrinterException as NfePrinterException, NfeRejectedException
    has_nfe = True
except ImportError:
    has_nfe = False

    class NfePrinterException(Exception):
        pass

    class NfeRejectedException(Exception):
        pass

try:
    from stoqsat.exceptions import PrinterException as SatPrinterException
    has_sat = True
except ImportError:
    has_sat = False

    class SatPrinterException(Exception):
        pass

_printer_lock = Semaphore()
log = logging.getLogger(__name__)
is_multiclient = False

TRANSPARENT_PIXEL = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII='  # noqa

WORKERS = []


def worker(f):
    """A marker for a function that should be threaded when the server executes.

    Usefull for regular checks that should be made on the server that will require warning the
    client
    """
    WORKERS.append(f)
    return f


def lock_printer(func):
    """Decorator to handle printer access locking.

    This will make sure that only one callsite is using the printer at a time.
    """
    def new_func(*args, **kwargs):
        if _printer_lock.locked():
            log.info('Waiting printer lock release in func %s' % func)

        with _printer_lock:
            return func(*args, **kwargs)

    return new_func


def get_plugin(manager, name):
    try:
        return manager.get_plugin(name)
    except PluginError:
        return None


class UnhandledMisconfiguration(Exception):
    pass


class _BaseResource(Resource):

    routes = []

    def get_json(self):
        if not request.data:
            return None
        return json.loads(request.data.decode(), parse_float=decimal.Decimal)

    def get_arg(self, attr, default=None):
        """Get the attr from querystring, form data or json"""
        # This is not working on all versions.
        if self.get_json():
            return self.get_json().get(attr, None)

        return request.form.get(attr, request.args.get(attr, default))

    def get_current_user(self, store):
        auth = request.headers.get('Authorization', '').split('Bearer ')
        token = AccessToken.get_by_token(store=store, token=auth[1])
        return token and token.user

    def get_current_station(self, store, token=None):
        if not token:
            auth = request.headers.get('Authorization', '').split('Bearer ')
            token = auth[1]
        token = AccessToken.get_by_token(store=store, token=token)
        return token and token.station

    def get_current_branch(self, store):
        station = self.get_current_station(store)
        return station and station.branch

    @classmethod
    def ensure_printer(cls, station, retries=20):
        assert _printer_lock.locked()

        store = api.get_default_store()
        device = DeviceSettings.get_by_station_and_type(store, station,
                                                        DeviceSettings.NON_FISCAL_PRINTER_DEVICE)
        if not device:
            # If we have no printer configured, there's nothing to ensure
            return

        # There is no need to lock the printer here, since it should already be locked by the
        # calling site of this method.
        # Test the printer to see if its working properly.
        printer = None
        try:
            printer = api.device_manager.printer
            return printer.is_drawer_open()
        except (SerialException, InvalidReplyException):
            if printer:
                printer._port.close()
            api.device_manager._printer = None
            for i in range(retries):
                log.info('Printer check failed. Reopening')
                try:
                    printer = api.device_manager.printer
                    printer.is_drawer_open()
                    break
                except SerialException:
                    gevent.sleep(1)
            else:
                # Reopening printer failed. re-raise the original exception
                raise

            # Invalidate the printer in the plugins so that it re-opens it
            manager = get_plugin_manager()

            # nfce does not need to reset the printer since it does not cache it.
            sat = get_plugin(manager, 'sat')
            if sat and sat.ui:
                sat.ui.printer = None

            nonfiscal = get_plugin(manager, 'nonfiscal')
            if nonfiscal and nonfiscal.ui:
                nonfiscal.ui.printer = printer

            return printer.is_drawer_open()


class DataResource(_BaseResource):
    """All the data the POS needs RESTful resource."""

    routes = ['/data']
    method_decorators = [login_required, store_provider]

    # All the tables get_data uses (directly or indirectly)
    watch_tables = ['sellable', 'product', 'storable', 'product_stock_item', 'branch_station',
                    'branch', 'login_user', 'sellable_category', 'client_category_price',
                    'payment_method', 'credit_provider']

    # Disabled @worker for now while testing gevent instead of threads
    def _postgres_listen(station):
        store = api.new_store()
        conn = store._connection._raw_connection
        conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
        cursor = store._connection.build_raw_cursor()
        cursor.execute("LISTEN update_te;")

        message = False
        while True:
            if select.select([conn], [], [], 5) != ([], [], []):
                conn.poll()
                while conn.notifies:
                    notify = conn.notifies.pop(0)
                    te_id, table = notify.payload.split(',')
                    # Update the data the client has when one of those changes
                    message = message or table in DataResource.watch_tables

            if message:
                EventStream.put_all({
                    'type': 'SERVER_UPDATE_DATA',
                    'data': DataResource.get_data(store)
                })
                message = False

    def _get_categories(self, store, station):
        categories_root = []
        aux = {}
        branch = self.get_current_branch(store)

        # SellableCategory and Sellable/Product data
        # FIXME: Remove categories that have no products inside them
        for c in store.find(SellableCategory):
            if c.category_id is None:
                parent_list = categories_root
            else:
                parent_list = aux.setdefault(
                    c.category_id, {}).setdefault('children', [])

            c_dict = aux.setdefault(c.id, {})
            parent_list.append(c_dict)

            # Set/Update the data
            c_dict.update({
                'id': c.id,
                'description': c.description,
            })
            c_dict.setdefault('children', [])
            products_list = c_dict.setdefault('products', [])

            tables = [Sellable, LeftJoin(Product, Product.id == Sellable.id)]

            if branch.person.company.cnpj.startswith('11.950.487'):
                # For now, only display products that have a fiscal configuration for the
                # current branch. We should find a better way to ensure this in the future
                tables.append(
                    Join(ProductBranchOverride,
                         And(ProductBranchOverride.product_id == Product.id,
                             ProductBranchOverride.branch_id == branch.id,
                             Ne(ProductBranchOverride.icms_template_id, None))))

            tables.append(
                LeftJoin(SellableBranchOverride,
                         And(SellableBranchOverride.sellable_id == Sellable.id,
                             SellableBranchOverride.branch_id == branch.id)))
            query = And(Sellable.category == c,
                        Eq(Coalesce(SellableBranchOverride.status, Sellable.status), "available"))

            # XXX: This should be modified for accepting generic keywords
            if station.type and station.type.name == 'auto':
                query = And(query, Sellable.keywords.like('%auto%'))

            sellables = store.using(*tables).find(Sellable, query).order_by('height', 'description')

            for s in sellables:
                ccp = store.find(ClientCategoryPrice, sellable_id=s.id)
                ccp_dict = {}
                for item in ccp:
                    ccp_dict[item.category_id] = str(item.price)

                products_list.append({
                    'id': s.id,
                    'code': s.code,
                    'barcode': s.barcode,
                    'description': s.description,
                    'price': str(s.price),
                    'order': str(s.product.height),
                    'category_prices': ccp_dict,
                    'color': s.product.part_number,
                    'availability': (
                        s.product and s.product.storable and
                        {
                            si.branch.id: str(si.quantity)
                            for si in s.product.storable.get_stock_items()
                        }
                    ),
                    'requires_kitchen_production': s.get_requires_kitchen_production(branch)
                })

            aux[c.id] = c_dict
        responses = signal('GetAdvancePaymentCategoryEvent').send()
        for response in responses:
            categories_root.append(response[1])

        return categories_root

    def _get_payment_methods(self, store):
        # PaymentMethod data
        payment_methods = []
        for pm in PaymentMethod.get_active_methods(store):
            if not pm.selectable():
                continue

            data = {'name': pm.method_name,
                    'max_installments': pm.max_installments}
            if pm.method_name == 'card':
                # FIXME: Add voucher
                data['card_types'] = [CreditCardData.TYPE_CREDIT,
                                      CreditCardData.TYPE_DEBIT]

            payment_methods.append(data)

        return payment_methods

    def _get_card_providers(self, store):
        providers = []
        for i in CreditProvider.get_card_providers(store):
            providers.append({'short_name': i.short_name, 'provider_id': i.provider_id})

        return providers

    def get_data(self, store):
        """Returns all data the POS needs to run

        This includes:

        - Which branch and statoin he is operating for
        - Current loged in user
        - What categories it has
            - What sellables those categories have
                - The stock amount for each sellable (if it controls stock)
        """
        station = self.get_current_station(store)
        user = self.get_current_user(store)
        staff_category = store.find(ClientCategory, ClientCategory.name == 'Staff').one()
        branch = station.branch
        config = get_config()
        can_send_sms = config.get("Twilio", "sid") is not None
        iti_discount = True if config.get("Discounts", "iti") == '1' else False
        hotjar_id = config.get("Hotjar", "id")

        sat_status = pinpad_status = printer_status = True
        if not is_multiclient:
            try:
                sat_status = check_sat()
            except LockFailedException:
                sat_status = True

            try:
                pinpad_status = check_pinpad()
            except LockFailedException:
                pinpad_status = True

            printer_status = None if check_drawer() is None else True

        # Current branch data
        retval = dict(
            branch=branch.id,
            branch_station=station.name,
            branch_object=dict(
                id=branch.id,
                name=branch.name,
                acronym=branch.acronym,
            ),
            station=dict(
                id=station.id,
                code=station.code,
                name=station.name,
                type=station.type.name if station.type else None,
                has_kps_enabled=station.has_kps_enabled,
            ),
            user_id=user and user.id,
            user=user and user.username,
            user_object=user and dict(
                id=user.id,
                name=user.username,
                person_name=user.person.name,
                profile_id=user.profile_id,
            ),
            categories=self._get_categories(store, station),
            payment_methods=self._get_payment_methods(store),
            providers=self._get_card_providers(store),
            staff_id=staff_category.id if staff_category else None,
            can_send_sms=can_send_sms,
            iti_discount=iti_discount,
            hotjar_id=hotjar_id,
            plugins=get_plugin_manager().active_plugins_names,
            # Device statuses
            sat_status=sat_status,
            pinpad_status=pinpad_status,
            printer_status=printer_status,
        )

        return retval

    def get(self, store):
        return self.get_data(store)


class DrawerResource(_BaseResource):
    """Drawer RESTful resource."""

    routes = ['/drawer']
    method_decorators = [login_required, store_provider]

    @lock_printer
    def get(self, store):
        """Get the current status of the drawer"""
        station = self.get_current_station(store)
        return self.ensure_printer(station)

    @lock_printer
    def post(self, store):
        """Send a signal to open the drawer"""
        if not api.device_manager.printer:
            raise UnhandledMisconfiguration('Printer not configured in this station')

        api.device_manager.printer.open_drawer()
        return 'success', 200


class PingResource(_BaseResource):
    """Ping RESTful resource."""

    routes = ['/ping']

    def get(self):
        return 'pong from stoqserver'


def format_cpf(document):
    return '%s.%s.%s-%s' % (document[0:3], document[3:6], document[6:9],
                            document[9:11])


def format_cnpj(document):
    return '%s.%s.%s/%s-%s' % (document[0:2], document[2:5], document[5:8],
                               document[8:12], document[12:])


def format_document(document):
    if len(document) == 11:
        return format_cpf(document)
    else:
        return format_cnpj(document)


class TillResource(_BaseResource):
    """Till RESTful resource."""
    routes = ['/till']
    method_decorators = [login_required]

    def _open_till(self, store, initial_cash_amount=0):
        station = self.get_current_station(store)
        last_till = Till.get_last(store, station)
        if not last_till or last_till.status != Till.STATUS_OPEN:
            # Create till and open
            till = Till(store=store, station=station, branch=station.branch)
            till.open_till(self.get_current_user(store))
            till.initial_cash_amount = decimal.Decimal(initial_cash_amount)
        else:
            # Error, till already opened
            assert False

    def _close_till(self, store, till_summaries, include_receipt_image=False):
        station = self.get_current_station(store)
        if not include_receipt_image:
            self.ensure_printer(station)
        # Here till object must exist
        till = Till.get_last(store, station)

        # Create TillSummaries
        till.get_day_summary()

        # Find TillSummary and store the user_value
        for till_summary in till_summaries:
            method = PaymentMethod.get_by_name(store, till_summary['method'])

            if till_summary['provider']:
                provider = store.find(CreditProvider, short_name=till_summary['provider']).one()
                summary = TillSummary.get_or_create(store, till=till, method=method.id,
                                                    provider=provider.id,
                                                    card_type=till_summary['card_type'])
            # Money method has no card_data or provider
            else:
                summary = TillSummary.get_or_create(store, till=till, method=method.id)

            summary.user_value = decimal.Decimal(till_summary['user_value'])

        balance = till.get_balance()
        if balance:
            till.add_debit_entry(balance, _('Blind till closing'))
        till.close_till(self.get_current_user(store))

        # The close till report will not be printed here, but can still be printed in
        # the frontend (using an image)
        if include_receipt_image:
            image = None
            responses = signal('GenerateTillClosingReceiptImageEvent').send(till)
            if (len(responses) == 1):  # Only nonfiscal plugin should answer this signal
                image = responses[0][1]
            return {'image': image}

    def _add_credit_or_debit_entry(self, store, data):
        # Here till object must exist
        till = Till.get_last(store, self.get_current_station(store))
        user = self.get_current_user(store)

        # FIXME: Check balance when removing to prevent negative till.
        if data['operation'] == 'debit_entry':
            reason = _('The user %s removed cash from till') % user.username
            till.add_debit_entry(decimal.Decimal(data['entry_value']), reason)
        elif data['operation'] == 'credit_entry':
            reason = _('The user %s supplied cash to the till') % user.username
            till.add_credit_entry(decimal.Decimal(data['entry_value']), reason)

    def _get_till_summary(self, store, till):
        payment_data = []
        for summary in till.get_day_summary():
            payment_data.append({
                'method': summary.method.method_name,
                'provider': summary.provider.short_name if summary.provider else None,
                'card_type': summary.card_type,
                'system_value': str(summary.system_value),
            })

        # XXX: We shouldn't create TIllSummaries since we are not closing the Till,
        # so we must rollback.
        store.rollback(close=False)

        return payment_data

    @lock_printer
    def post(self):
        data = self.get_json()
        with api.new_store() as store:
            # Provide responsible
            if data['operation'] == 'open_till':
                reply = self._open_till(store, data['initial_cash_amount'])
            elif data['operation'] == 'close_till':
                reply = self._close_till(store,
                                         data['till_summaries'], data['include_receipt_image'])
            elif data['operation'] in ['debit_entry', 'credit_entry']:
                reply = self._add_credit_or_debit_entry(store, data)
            else:
                raise AssertionError('Unkown till operation %r', data['operation'])

        return reply

    def get(self):
        # Retrieve Till data
        with api.new_store() as store:
            till = Till.get_last(store, self.get_current_station(store))

            if not till:
                return None

            # Checks the remaining time available for till to be open
            if till.needs_closing():
                expiration_time_in_seconds = 0
            else:
                # Till must be closed on the next day (midnight) + tolerance time
                opening_date = till.opening_date.replace(hour=0, minute=0, second=0, microsecond=0)
                tolerance = api.sysparam.get_int('TILL_TOLERANCE_FOR_CLOSING')
                next_close = opening_date + datetime.timedelta(days=1, hours=tolerance)
                expiration_time_in_seconds = (next_close - localnow()).seconds

            till_data = {
                'status': till.status,
                'opening_date': till.opening_date.strftime('%Y-%m-%d'),
                'closing_date': (till.closing_date.strftime('%Y-%m-%d') if
                                 till.closing_date else None),
                'initial_cash_amount': str(till.initial_cash_amount),
                'final_cash_amount': str(till.final_cash_amount),
                # Get payments data that will be used on 'close_till' action.
                'entry_types': till.status == 'open' and self._get_till_summary(store, till) or [],
                'expiration_time_in_seconds': expiration_time_in_seconds  # seconds
            }

        return till_data


class ClientResource(_BaseResource):
    """Client RESTful resource."""
    routes = ['/client']

    def _dump_client(self, client):
        person = client.person
        birthdate = person.individual.birth_date if person.individual else None

        saleviews = person.client.get_client_sales().order_by(Desc('confirm_date'))
        last_items = {}
        for saleview in saleviews:
            for item in saleview.sale.get_items():
                last_items[item.sellable_id] = item.sellable.description
                # Just the last 3 products the client bought
                if len(last_items) == 3:
                    break

        if person.company:
            doc = person.company.cnpj
        else:
            doc = person.individual.cpf

        category_name = client.category.name if client.category else ""

        data = dict(
            id=client.id,
            category=client.category_id,
            doc=doc,
            last_items=last_items,
            name=person.name,
            birthdate=birthdate,
            category_name=category_name,
        )

        return data

    def _get_by_doc(self, store, data, doc):
        # Extra precaution in case we ever send the cpf already formatted
        document = format_cpf(raw_document(doc))

        person = Person.get_by_document(store, document)
        if person and person.client:
            data = self._dump_client(person.client)

        # Plugins that listen to this signal will return extra fields
        # to be added to the response
        responses = signal('CheckRewardsPermissionsEvent').send(doc)
        for response in responses:
            data.update(response[1])

        return data

    def _get_by_category(self, store, category_name):
        tables = [Client,
                  Join(ClientCategory, Client.category_id == ClientCategory.id)]
        clients = store.using(*tables).find(Client, ClientCategory.name == category_name)
        retval = []
        for client in clients:
            retval.append(self._dump_client(client))
        return retval

    def post(self):
        data = self.get_json()

        with api.new_store() as store:
            if data.get('doc'):
                return self._get_by_doc(store, data, data['doc'])
            elif data.get('category_name'):
                return self._get_by_category(store, data['category_name'])
        return data


class ExternalClientResource(_BaseResource):
    """Information about a client from external services, such as Passbook"""
    routes = ['/extra_client_info/<doc>']

    def get(self, doc):
        # Extra precaution in case we ever send the cpf already formatted
        doc = format_cpf(raw_document(doc))
        responses = signal('GetClientInfoEvent').send(doc)

        data = dict()
        for response in responses:
            data.update(response[1])
        return data


class LoginResource(_BaseResource):
    """Login RESTful resource."""

    routes = ['/login']
    method_decorators = [store_provider]

    def post(self, store):
        username = self.get_arg('user')
        pw_hash = self.get_arg('pw_hash')
        station_name = self.get_arg('station_name')

        station = store.find(BranchStation, name=station_name, is_active=True).one()
        global PDV_VERSION
        PDV_VERSION = request.args.get('pdv_version')
        if not station:
            abort(401)

        try:
            # FIXME: Respect the branch the user is in.
            user = LoginUser.authenticate(store, username, pw_hash, current_branch=None)
            provide_utility(ICurrentUser, user, replace=True)
        except LoginError as e:
            abort(403, str(e))

        token = AccessToken.get_or_create(store, user, station).token
        return jsonify({
            "token": "JWT {}".format(token),
            "user": {"id": user.id},
        })


class LogoutResource(_BaseResource):

    routes = ['/logout']
    method_decorators = [store_provider]

    def post(self, store):
        token = self.get_arg('token')
        token = token and token.split(' ')
        token = token[1] if len(token) == 2 else None

        if not token:
            abort(401)

        token = AccessToken.get_by_token(store=store, token=token)
        if not token:
            abort(403, "invalid token")
        token.revoke()

        return jsonify({"message": "successfully revoked token"})


class AuthResource(_BaseResource):
    """Authenticate a user agasint the database.

    This will not replace the ICurrentUser. It will just validate if a login/password is valid.
    """

    routes = ['/auth']
    method_decorators = [login_required, store_provider]

    def post(self, store):
        username = self.get_arg('user')
        pw_hash = self.get_arg('pw_hash')
        permission = self.get_arg('permission')

        try:
            # FIXME: Respect the branch the user is in.
            user = LoginUser.authenticate(store, username, pw_hash, current_branch=None)
        except LoginError as e:
            return make_response(str(e), 403)

        if user.profile.check_app_permission(permission):
            return True
        return make_response(_('User does not have permission'), 403)


class EventStream(_BaseResource):
    """A stream of events from this server to the application.

    Callsites can use EventStream.put(event) to send a message from the server to the client
    asynchronously.

    Note that there should be only one client connected at a time. If more than one are connected,
    all of them will receive all events
    """
    _streams = {}
    has_stream = Event()

    routes = ['/stream']

    @classmethod
    def put(cls, station, data):
        """Put a event only on the client stream"""
        # Wait until we have at least one stream
        cls.has_stream.wait()

        # Put event only on client stream
        stream = cls._streams.get(station.id)
        if stream:
            stream.put(data)

    @classmethod
    def put_all(cls, data):
        """Put a event in all streams"""
        # Wait until we have at least one stream
        cls.has_stream.wait()

        # Put event in all streams
        for stream in cls._streams.values():
            stream.put(data)

    def _loop(self, stream):
        while True:
            data = stream.get()
            yield "data: " + json.dumps(data, cls=JsonEncoder) + "\n\n"

    def get(self):
        stream = Queue()
        station = self.get_current_station(api.get_default_store(), token=request.args['token'])
        self._streams[station.id] = stream
        self.has_stream.set()

        # If we dont put one event, the event stream does not seem to get stabilished in the browser
        stream.put(json.dumps({}))

        # This is the best time to check if there are pending transactions, since the frontend just
        # stabilished a connection with the backend (thats us).
        has_canceled = TefCheckPendingEvent.send()
        if has_canceled and has_canceled[0][1]:
            EventStream.put(station, {'type': 'TEF_WARNING_MESSAGE',
                                      'message': ('Última transação TEF não foi efetuada.'
                                                  ' Favor reter o Cupom.')})
            EventStream.put(station, {'type': 'CLEAR_SALE'})
        return Response(self._loop(stream), mimetype="text/event-stream")


class TefResource(_BaseResource):
    routes = ['/tef/<signal_name>']
    method_decorators = [login_required, store_provider]

    waiting_reply = Event()
    reply = Queue()

    @lock_printer
    def _print_callback(self, lib, holder, merchant):
        printer = api.device_manager.printer
        if not printer:
            return

        # TODO: Add paramter to control if this will be printed or not
        if merchant:
            printer.print_line(merchant)
            printer.cut_paper()
        if holder:
            printer.print_line(holder)
            printer.cut_paper()

    def _message_callback(self, lib, message, can_abort=False):
        station = self.get_current_station(api.get_default_store())
        EventStream.put(station, {
            'type': 'TEF_DISPLAY_MESSAGE',
            'message': message,
            'can_abort': can_abort,
        })

        # tef library (ntk/sitef) has some blocking calls (specially pinpad comunication).
        # Before returning, we need to briefly hint gevent to let the EventStream co-rotine run,
        # so that the message above can be sent to the frontend.
        gevent.sleep(0.001)

    def _question_callback(self, lib, question):
        station = self.get_current_station(api.get_default_store())
        EventStream.put(station, {
            'type': 'TEF_ASK_QUESTION',
            'data': question,
        })

        log.info('Waiting tef reply')
        self.waiting_reply.set()
        reply = self.reply.get()
        log.info('Got tef reply: %s', reply)
        self.waiting_reply.clear()

        return reply

    @lock_pinpad(block=True)
    def post(self, store, signal_name):
        station = self.get_current_station(store)
        if signal_name not in ['StartTefSaleSummaryEvent', 'StartTefAdminEvent']:
            till = Till.get_last(store, station)
            if not till or till.status != Till.STATUS_OPEN:
                raise TillError(_('There is no till open'))

        try:
            with _printer_lock:
                self.ensure_printer(station)
        except Exception:
            EventStream.put(station, {
                'type': 'TEF_OPERATION_FINISHED',
                'success': False,
                'message': 'Erro comunicando com a impressora',
            })
            return

        signal('TefMessageEvent').connect(self._message_callback)
        signal('TefQuestionEvent').connect(self._question_callback)
        signal('TefPrintEvent').connect(self._print_callback)

        operation_signal = signal(signal_name)
        # There should be just one plugin connected to this event.
        assert len(operation_signal.receivers) == 1, operation_signal

        data = self.get_json()
        # Remove origin from data, if present
        data.pop('origin', None)
        try:
            # This operation will be blocked here until its complete, but since we are running
            # each request using threads, the server will still be available to handle other
            # requests (specially when handling comunication with the user through the callbacks
            # above)
            log.info('send tef signal %s (%s)', signal_name, data)
            retval = operation_signal.send(station=station, **data)[0][1]
            message = retval['message']
        except Exception as e:
            retval = False
            log.info('Tef failed: %s', str(e))
            if len(e.args) == 2:
                message = e.args[1]
            else:
                message = 'Falha na operação'

        EventStream.put(station, {
            'type': 'TEF_OPERATION_FINISHED',
            'success': retval,
            'message': message,
        })


class TefReplyResource(_BaseResource):
    routes = ['/tef/reply']
    method_decorators = [login_required]

    def post(self):
        assert TefResource.waiting_reply.is_set()

        data = self.get_json()
        TefResource.reply.put(json.loads(data['value']))


class TefCancelCurrentOperation(_BaseResource):
    routes = ['/tef/abort']
    method_decorators = [login_required]

    def post(self):
        signal('TefAbortOperationEvent').send()


class ImageResource(_BaseResource):
    """Image RESTful resource."""

    routes = ['/image/<id>']

    def get(self, id):
        is_main = bool(request.args.get('is_main', None))
        keyword_filter = request.args.get('keyword')
        # FIXME: The images should store tags so they could be requested by that tag and
        # product_id. At the moment, we simply check if the image is main or not and
        # return the first one.
        with api.new_store() as store:
            images = store.find(Image, sellable_id=id, is_main=is_main)
            if keyword_filter:
                images = images.find(Image.keywords.like('%{}%'.format(keyword_filter)))
            image = images.any()
            if image:
                return send_file(io.BytesIO(image.image), mimetype='image/png')
            else:
                response = make_response(base64.b64decode(TRANSPARENT_PIXEL))
                response.headers.set('Content-Type', 'image/jpeg')
                return response


class SaleResourceMixin:
    """Mixin class that provides common methods for sale/advance_payment

    This includes:

        - Payment creation
        - Client verification
        - Sale/Advance already saved checking
    """

    def _check_already_saved(self, store, klass, obj_id):
        existing_sale = store.get(klass, obj_id)
        if existing_sale:
            log.info('Sale already saved: %s' % obj_id)
            log.info('send CheckCouponTransmittedEvent signal')
            # XXX: This might not really work for AdvancePayment, we need to test this. It might
            # need specific handling.
            is_coupon_transmitted = signal('CheckCouponTransmittedEvent').send(existing_sale)[0][1]
            if is_coupon_transmitted:
                return self._handle_coupon_printing_fail(existing_sale)
            raise AssertionError(_('Sale already saved'))

    def _create_client(self, store, document, data):
        # Use data to get name from passbook
        name = data.get('client_name', _('No name'))
        person = Person(store=store, name=name)
        Individual(store=store, person=person, cpf=document)
        client = Client(store=store, person=person)
        return client

    def _get_client_and_document(self, store, data):
        client_id = data.get('client_id')
        # We remove the format of the document and then add it just
        # as a precaution in case it comes not formatted
        coupon_document = raw_document(data.get('coupon_document', '') or '')
        if coupon_document:
            coupon_document = format_document(coupon_document)
        client_document = raw_document(data.get('client_document', '') or '')
        if client_document:
            client_document = format_document(client_document)

        if client_id:
            client = store.get(Client, client_id)
        elif client_document:
            person = Person.get_by_document(store, client_document)
            if person and person.client:
                client = person.client
            elif person and not person.client:
                client = Client(store=store, person=person)
            else:
                client = None
        else:
            client = None

        return client, client_document, coupon_document

    def _handle_coupon_printing_fail(self, obj):
        log.exception('Error printing coupon')
        # XXX: Rever string
        message = _("Sale {sale_identifier} confirmed but printing coupon failed")
        return {
            # XXX: This is not really an error, more of a partial success were the coupon
            # (sat/nfce) was emitted, but the printing of the coupon failed. The frontend should
            # present to the user the option to try again or send the coupom via sms/email
            'error_type': 'printing',
            'message': message.format(sale_identifier=obj.identifier),
            'sale_id': obj.id
        }, 201

    def _get_card_device(self, store, name):
        device = store.find(CardPaymentDevice, description=name).any()
        if not device:
            device = CardPaymentDevice(store=store, description=name)
        return device

    def _get_provider(self, store, name):
        if not name:
            name = _("UNKNOWN")
        received_name = name.strip()
        name = PROVIDER_MAP.get(received_name, received_name)
        provider = store.find(CreditProvider, provider_id=name).one()
        if not provider:
            provider = CreditProvider(store=store, short_name=name, provider_id=name)
            log.info('Could not find a provider named %s', name)
        else:
            log.info('Fixing card name from %s to %s', received_name, name)
        return provider

    def _create_payments(self, store, group, branch, station, sale_total, payment_data):
        money_payment = None
        payments_total = 0
        for p in payment_data:
            method_name = p['method']
            tef_data = p.get('tef_data', {})
            if method_name == 'tef':
                p['provider'] = tef_data['card_name']
                method_name = 'card'

            method = PaymentMethod.get_by_name(store, method_name)
            installments = p.get('installments', 1) or 1

            due_dates = list(create_date_interval(
                INTERVALTYPE_MONTH,
                interval=1,
                start_date=localnow(),
                count=installments))

            payment_value = currency(p['value'])
            payments_total += payment_value

            p_list = method.create_payments(
                branch, station, Payment.TYPE_IN, group,
                payment_value, due_dates)

            if method.method_name == 'money':
                # FIXME Frontend should not allow more than one money payment. this can be changed
                # once https://gitlab.com/stoqtech/private/bdil/issues/75 is fixed?
                if not money_payment or payment_value > money_payment.value:
                    money_payment = p_list[0]
            elif method.method_name == 'card':
                for payment in p_list:
                    card_data = method.operation.get_card_data_by_payment(payment)

                    card_type = p['card_type']
                    # This card_type does not exist in stoq. Change it to 'credit'.
                    if card_type not in CreditCardData.types:
                        log.info('Invalid card type %s. changing to credit', card_type)
                        card_type = 'credit'
                    # FIXME Stoq already have the voucher concept, but we should keep this for a
                    # little while for backwars compatibility
                    elif card_type == 'voucher':
                        card_type = 'debit'
                    provider = self._get_provider(store, p['provider'])

                    if tef_data:
                        card_data.nsu = tef_data['nsu']
                        card_data.auth = tef_data['auth']
                        authorizer = tef_data.get('authorizer', 'TEF')
                        device = self._get_card_device(store, authorizer)
                    else:
                        device = self._get_card_device(store, 'POS')

                    card_data.update_card_data(device, provider, card_type, installments)
                    card_data.te.metadata = tef_data

        # If payments total exceed sale total, we must adjust money payment so that the change is
        # correctly calculated..
        if payments_total > sale_total and money_payment:
            money_payment.value -= (payments_total - sale_total)
            assert money_payment.value >= 0, money_payment.value


class SaleResource(_BaseResource, SaleResourceMixin):
    """Sellable category RESTful resource."""

    routes = ['/sale', '/sale/<string:sale_id>']
    method_decorators = [login_required, store_provider]

    def _handle_nfe_coupon_rejected(self, sale, reason):
        log.exception('NFC-e sale rejected')
        message = _("NFC-e of sale {sale_identifier} was rejected")
        return {
            'error_type': 'rejection',
            'message': message.format(sale_identifier=sale.identifier),
            'sale_id': sale.id,
            'reason': reason
        }, 201

    def _encode_payments(self, payments):
        return [{'method': p.method.method_name,
                 'value': str(p.value)} for p in payments]

    def _encode_items(self, items):
        return [{'quantity': str(i.quantity),
                 'price': str(i.price),
                 'description': i.get_description()} for i in items]

    def _nfe_progress_event(self, message):
        station = self.get_current_station(api.get_default_store())
        EventStream.put(station, {'type': 'NFE_PROGRESS', 'message': message})

    def _nfe_warning_event(self, message, details):
        station = self.get_current_station(api.get_default_store())
        EventStream.put(station, {'type': 'NFE_WARNING', 'message': message, 'details': details})

    def _nfe_success_event(self, message, details=None):
        station = self.get_current_station(api.get_default_store())
        EventStream.put(station, {'type': 'NFE_SUCCESS', 'message': message, 'details': details})

    @lock_printer
    @lock_sat(block=True)
    def post(self, store):
        # FIXME: Check branch state and force fail if no override for that product is present.
        data = self.get_json()
        products = data['products']
        client_category_id = data.get('price_table')
        should_print_receipts = data.get('print_receipts', True)

        client, client_document, coupon_document = self._get_client_and_document(store, data)

        sale_id = data.get('sale_id')
        early_response = self._check_already_saved(store, Sale, sale_id)
        if early_response:
            return early_response

        # Print the receipts and confirm the transaction before anything else. If the sale fails
        # (either by a sat device error or a nfce conectivity/rejection issue), the tef receipts
        # will still be printed/confirmed and the user can finish the sale or the client.
        TefPrintReceiptsEvent.send(sale_id)

        # Create the sale
        branch = self.get_current_branch(store)
        station = self.get_current_station(store)
        user = self.get_current_user(store)
        group = PaymentGroup(store=store)
        sale = Sale(
            store=store,
            id=sale_id,
            branch=branch,
            station=station,
            salesperson=user.person.sales_person,
            client=client,
            client_category_id=client_category_id,
            group=group,
            open_date=localnow(),
            coupon_id=None,
        )

        # Add products
        for p in products:
            sellable = store.get(Sellable, p['id'])
            item = sale.add_sellable(sellable, price=currency(p['price']),
                                     quantity=decimal.Decimal(p['quantity']))
            # XXX: bdil has requested that when there is a special discount, the discount does
            # not appear on the coupon. Instead, the item wil be sold using the discount price
            # as the base price. Maybe this should be a parameter somewhere
            item.base_price = item.price

        # Add payments
        self._create_payments(store, group, branch, station,
                              sale.get_total_sale_amount(), data['payments'])

        # Confirm the sale
        group.confirm()
        sale.order(user)

        external_order_id = data.get('external_order_id')
        if external_order_id:
            ProcessExternalOrderEvent.send(sale, external_order_id=external_order_id)

        till = Till.get_last(store, station)
        if till.status != Till.STATUS_OPEN:
            raise TillError(_('There is no till open'))

        sale.confirm(user, till)

        GrantLoyaltyPointsEvent.send(sale, document=(client_document or coupon_document))

        if has_nfe:
            NfeProgressEvent.connect(self._nfe_progress_event)
            NfeWarning.connect(self._nfe_warning_event)
            NfeSuccess.connect(self._nfe_success_event)

        # Fiscal plugins will connect to this event and "do their job"
        # It's their responsibility to raise an exception in case of any error
        try:
            SaleConfirmedRemoteEvent.emit(sale, coupon_document, should_print_receipts)
        except (NfePrinterException, SatPrinterException):
            return self._handle_coupon_printing_fail(sale)
        except NfeRejectedException as e:
            return self._handle_nfe_coupon_rejected(sale, e.reason)

        if not sale.station.has_kps_enabled or not sale.get_kitchen_items():
            return True

        order_number = data.get('order_number')
        if order_number in {'0', '', None}:
            abort(400, "Invalid order number")

        log.info('emitting event PrintKitchenCouponEvent {}'.format(order_number))
        PrintKitchenCouponEvent.send(sale, order_number=order_number)
        return True

    def get(self, store, sale_id):
        sale = store.get(Sale, sale_id)
        if not sale:
            abort(404)
        transmitted = signal('CheckCouponTransmittedEvent').send(sale)
        is_coupon_transmitted = transmitted[0][1] if transmitted else False
        return {
            'id': sale.id,
            'confirm_date': str(sale.confirm_date),
            'items': self._encode_items(sale.get_items()),
            'total': str(sale.total_amount),
            'payments': self._encode_payments(sale.payments),
            'client': sale.get_client_name(),
            'status': sale.status_str,
            'transmitted': is_coupon_transmitted,
        }, 200

    def delete(self, store, sale_id):
        # This is not really 'deleting' a sale, but informing us that a sale was never confirmed
        # this is necessary since we can create payments for a sale before it actually exists, those
        # paymenst might need to be canceled
        signal('SaleAbortedEvent').send(sale_id)


class AdvancePaymentResource(_BaseResource, SaleResourceMixin):

    routes = ['/advance_payment']
    method_decorators = [login_required, store_provider]

    @lock_printer
    def post(self, store):
        # We need to delay this import since the plugin will only be in the path after stoqlib
        # initialization
        from stoqpassbook.domain import AdvancePayment
        data = self.get_json()
        client, client_document, coupon_document = self._get_client_and_document(store, data)
        if not client:
            client = self._create_client(store, client_document, data)

        advance_id = data.get('sale_id')
        early_response = self._check_already_saved(store, AdvancePayment, advance_id)
        if early_response:
            return early_response

        total = 0
        for p in data['products']:
            total += currency(p['price']) * decimal.Decimal(p['quantity'])

        # Print the receipts and confirm the transaction before anything else. If the sale fails
        # (either by a sat device error or a nfce conectivity/rejection issue), the tef receipts
        # will still be printed/confirmed and the user can finish the sale or the client.
        TefPrintReceiptsEvent.send(advance_id)

        branch = self.get_current_branch(store)
        station = self.get_current_station(store)
        user = self.get_current_user(store)
        group = PaymentGroup(store=store)
        advance = AdvancePayment(
            id=advance_id,
            store=store,
            client=client,
            total_value=total,
            branch=branch,
            station=station,
            group=group,
            responsible=user)

        # Add payments
        self._create_payments(store, group, branch, station, advance.total_value, data['payments'])
        till = Till.get_last(store, station)
        if not till or till.status != Till.STATUS_OPEN:
            raise TillError(_('There is no till open'))
        advance.confirm(till)

        GrantLoyaltyPointsEvent.send(advance, document=(client_document or coupon_document))

        # FIXME: We still need to implement the receipt in non-fiscal plugin
        try:
            PrintAdvancePaymentReceiptEvent.send(advance, document=coupon_document)
        except Exception:
            return self._handle_coupon_printing_fail(advance)

        return True


class AdvancePaymentCouponImageResource(_BaseResource):

    routes = ['/advance_payment/<string:id>/coupon']
    method_decorators = [login_required, store_provider]

    def get(self, store, id):
        responses = GenerateAdvancePaymentReceiptPictureEvent.send(id)

        if len(responses) == 0:
            abort(400)

        return {
            'image': responses[0][1],
        }, 200


class PrintCouponResource(_BaseResource):
    """Image RESTful resource."""

    routes = ['/sale/<sale_id>/print_coupon']
    method_decorators = [login_required, store_provider]

    @lock_printer
    def get(self, store, sale_id):
        self.ensure_printer(self.get_current_station(store))

        sale = store.get(Sale, sale_id)
        signal('PrintCouponCopyEvent').send(sale)


class SaleCouponImageResource(_BaseResource):

    routes = ['/sale/<string:sale_id>/coupon']
    method_decorators = [login_required, store_provider]

    def get(self, store, sale_id):
        sale = store.get(Sale, sale_id)
        if not sale:
            abort(400)

        responses = GenerateInvoicePictureEvent.send(sale)
        assert len(responses) >= 0
        return {
            'image': responses[0][1],
        }, 200


class SmsResource(_BaseResource):
    """SMS RESTful resource."""
    routes = ['/sale/<sale_id>/send_coupon_sms']
    method_decorators = [login_required, store_provider]

    def _send_sms(self, to, message):
        config = get_config()
        sid = config.get('Twilio', 'sid')
        secret = config.get('Twilio', 'secret')
        from_phone_number = config.get('Twilio', 'from')

        sms_data = {"From": from_phone_number, "To": to, "Body": message}

        r = requests.post('https://api.twilio.com/2010-04-01/Accounts/%s/Messages.json' % sid,
                          data=sms_data, auth=(sid, secret))
        return r.text

    def post(self, store, sale_id):
        GetCouponSmsTextEvent = signal('GetCouponSmsTextEvent')
        assert len(GetCouponSmsTextEvent.receivers) == 1

        sale = store.get(Sale, sale_id)
        message = GetCouponSmsTextEvent.send(sale)[0][1]
        to = '+55' + self.get_json()['phone_number']
        return self._send_sms(to, message)


@lock_printer
def check_drawer():
    try:
        return DrawerResource.ensure_printer(get_current_station(), retries=1)
    except (SerialException, InvalidReplyException):
        return None


@worker
def check_drawer_loop(station):
    # default value of is_open
    is_open = ''

    # Check every second if it is opened.
    # Alert only if changes.
    while True:
        new_is_open = check_drawer()

        if is_open != new_is_open:
            message = {
                True: 'DRAWER_ALERT_OPEN',
                False: 'DRAWER_ALERT_CLOSE',
                None: 'DRAWER_ALERT_ERROR',
            }
            EventStream.put(station, {
                'type': message[new_is_open],
            })
            status_printer = None if new_is_open is None else True
            EventStream.put(station, {
                'type': 'DEVICE_STATUS_CHANGED',
                'device': 'printer',
                'status': status_printer,
            })
            is_open = new_is_open

        gevent.sleep(1)


@lock_sat(block=False)
def check_sat():
    if len(CheckSatStatusEvent.receivers) == 0:
        # No SAT was found, what means there is no need to warn front-end there is a missing
        # or broken SAT
        return True

    event_reply = CheckSatStatusEvent.send()
    return event_reply and event_reply[0][1]


@worker
def check_sat_loop(station):
    if len(CheckSatStatusEvent.receivers) == 0:
        return

    sat_ok = -1

    while True:
        try:
            new_sat_ok = check_sat()
        except LockFailedException:
            # Keep previous state.
            new_sat_ok = sat_ok

        if sat_ok != new_sat_ok:
            EventStream.put(station, {
                'type': 'DEVICE_STATUS_CHANGED',
                'device': 'sat',
                'status': new_sat_ok,
            })
            sat_ok = new_sat_ok

        gevent.sleep(60 * 5)


@lock_pinpad(block=False)
def check_pinpad():
    event_reply = CheckPinpadStatusEvent.send()
    return event_reply and event_reply[0][1]


@worker
def check_pinpad_loop(station):
    pinpad_ok = -1

    while True:
        try:
            new_pinpad_ok = check_pinpad()
        except LockFailedException:
            # Keep previous state.
            new_pinpad_ok = pinpad_ok

        if pinpad_ok != new_pinpad_ok:
            EventStream.put(station, {
                'type': 'DEVICE_STATUS_CHANGED',
                'device': 'pinpad',
                'status': new_pinpad_ok,
            })
            pinpad_ok = new_pinpad_ok

        gevent.sleep(60)


@worker
def inform_till_status(station):
    while True:
        # Wait for the new work day
        now = datetime.datetime.now()
        update_time = datetime.datetime(now.year, now.month, now.day, hour=6, minute=0)
        wait_time = (update_time + datetime.timedelta(days=1) - now).total_seconds()
        gevent.sleep(max(60, wait_time))

        # Send an action to inform the front-end the status of the last opened till, to warn the
        # user if it needs to be closed
        with api.new_store() as store:
            EventStream.put(station, {
                'type': 'CHECK_TILL_FINISHED',
                'lastTill': {'status': Till.get_last(store, station).status}
            })


@worker
def post_ping_request(station):
    if is_developer_mode():
        return

    target = 'https://app.stoq.link:9000/api/ping'
    time_format = '%d-%m-%Y %H:%M:%S%Z'
    store = api.get_default_store()
    plugin_manager = get_plugin_manager()
    boot_time = datetime.datetime.fromtimestamp(psutil.boot_time()).strftime(time_format)

    def get_stoq_conf():
        with open(get_config().get_filename(), 'r') as fh:
            return fh.read().encode()

    def get_clisitef_ini():
        try:
            with open('CliSiTef.ini', 'r') as fh:
                return fh.read().encode()
        except FileNotFoundError:
            return ''.encode()

    while True:
        try:
            dpkg_list = subprocess.check_output('dpkg -l \\*stoq\\*', shell=True).decode()
        except subprocess.CalledProcessError:
            dpkg_list = ""
        stoq_packages = re.findall(r'ii\s*(\S*)\s*(\S*)', dpkg_list)
        if PDV_VERSION:
            log.info('Running stoq_pdv {}'.format(PDV_VERSION))
        log.info('Running stoq {}'.format(stoq_version))
        log.info('Running stoq-server {}'.format(stoqserver_version))
        log.info('Running stoqdrivers {}'.format(stoqdrivers_version))
        local_time = tzlocal.get_localzone().localize(datetime.datetime.now())

        response = requests.post(
            target,
            headers={'Stoq-Backend': '{}-portal'.format(api.sysparam.get_string('USER_HASH'))},
            data={
                'station_id': station.id,
                'data': json.dumps({
                    'platform': {
                        'architecture': platform.architecture(),
                        'distribution': platform.dist(),
                        'system': platform.system(),
                        'uname': platform.uname(),
                        'python_version': platform.python_version_tuple(),
                        'postgresql_version': get_database_version(store)
                    },
                    'system': {
                        'boot_time': boot_time,
                        'cpu_times': psutil.cpu_times(),
                        'load_average': os.getloadavg(),
                        'disk_usage': psutil.disk_usage('/'),
                        'virtual_memory': psutil.virtual_memory(),
                        'swap_memory': psutil.swap_memory()
                    },
                    'plugins': {
                        'available': plugin_manager.available_plugins_names,
                        'installed': plugin_manager.installed_plugins_names,
                        'active': plugin_manager.active_plugins_names,
                        'versions': getattr(plugin_manager, 'available_plugins_versions', None)
                    },
                    'running_versions': {
                        'pdv': PDV_VERSION,
                        'stoq': stoq_version,
                        'stoqserver': stoqserver_version,
                        'stoqdrivers': stoqdrivers_version
                    },
                    'stoq_packages': dict(stoq_packages),
                    'local_time': local_time.strftime(time_format),
                    'stoq_conf_md5': md5(get_stoq_conf()).hexdigest(),
                    'clisitef_ini_md5': md5(get_clisitef_ini()).hexdigest()
                })
            }
        )

        log.info("POST {} {} {}".format(
            target,
            response.status_code,
            response.elapsed.total_seconds()))
        gevent.sleep(3600)
