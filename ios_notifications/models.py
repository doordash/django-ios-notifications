# -*- coding: utf-8 -*-
import socket
import struct
from binascii import hexlify, unhexlify
import datetime

from django.db import models
from django.contrib.auth.models import User
from django.utils import simplejson as json

import OpenSSL


class NotificationPayloadSizeExceeded(Exception):
    message = 'The notification maximum payload size of 256 bytes was exceeded'


class NotConnectedException(Exception):
    message = 'You must open a socket connection before writing a message'


class BaseService(models.Model):
    """
    A base service class intended to be subclassed.
    """
    name = models.CharField(max_length=255)
    hostname = models.CharField(max_length=255, unique=True)
    PORT = 0  # Should be overriden by subclass
    connection = None

    def connect(self, certificate, private_key):
        """
        Establishes an encrypted SSL socket connection to the service.
        After connecting the socket can be written to or read from.
        """
        # ssl in Python < 3.2 does not support certificates/keys as strings.
        # See http://bugs.python.org/issue3823
        # Therefore pyOpenSSL which lets us do this is a dependancy.
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        cert = OpenSSL.crypto.load_certificate(OpenSSL.crypto.FILETYPE_PEM, certificate)
        pkey = OpenSSL.crypto.load_privatekey(OpenSSL.crypto.FILETYPE_PEM, private_key)
        context = OpenSSL.SSL.Context(OpenSSL.SSL.SSLv3_METHOD)
        context.use_certificate(cert)
        context.use_privatekey(pkey)
        self.connection = OpenSSL.SSL.Connection(context, sock)
        self.connection.connect((self.hostname, self.PORT))
        self.connection.set_connect_state()
        try:
            self.connection.do_handshake()
            return True
        except Exception as e:
            print e, e.__class__
        return False

    def disconnect(self):
        if self.connection is not None:
            self.connection.shutdown()
            self.connection.close()

    class Meta:
        abstract = True


class APNService(BaseService):
    """
    Represents an Apple Notification Service either for live
    or sandbox notifications.

    `private_key` is optional if both the certificate and key are provided in
    `certificate`.
    """
    certificate = models.TextField()
    private_key = models.TextField()

    PORT = 2195
    fmt = '!cH32sH%ds'

    def connect(self):
        return super(APNService, self).connect(self.certificate, self.private_key)

    def push_notification_to_devices(self, notification, devices=None):
        """
        Sends the specific notification to devices.
        if `devices` is not supplied, all devices in the `APNService`'s device
        list will be sent the notification.
        """
        if devices is None:
            devices = self.device_set.filter(is_active=True)
        if self.connect():
            self._write_message(notification, devices)
            self.disconnect()

    def _write_message(self, notification, devices):
        if not isinstance(notification, Notification):
            raise TypeError('notification should be an instance of ios_notifications.models.Notification')

        if self.connection is None:
            if not self.connect():
                return

        aps = {'alert': notification.message}
        if notification.badge is not None:
            aps['badge'] = notification.badge
        if notification.sound is not None:
            aps['sound'] = notification.sound

        message = {'aps': aps}
        payload = json.dumps(message, separators=(',', ':'))

        if len(payload) > 256:
            raise NotificationPayloadSizeExceeded

        for device in devices:
            try:
                self.connection.send(self.pack_message(payload, device))
            except OpenSSL.SSL.WantWriteError:
                self.disconnect()
                i = devices.index(device)
                if isinstance(devices, models.query.QuerySet):
                    devices.update(last_notified_at=datetime.datetime.now())
                devices[:i].update(last_notified_at=datetime.datetime.now())
                return self._write_message(notification, devices[i:]) if self.connect() else None
        if isinstance(devices, models.query.QuerySet):
            devices.update(last_notified_at=datetime.datetime.now())
        notification.last_sent_at = datetime.datetime.now()
        notification.save()

    def pack_message(self, payload, device):
        if len(payload) > 256:
            raise NotificationPayloadSizeExceeded
        if not isinstance(device, Device):
            raise TypeError('device must be an instance of ios_notifications.models.Device')

        msg = struct.pack(self.fmt % len(payload), chr(0), 32, unhexlify(device.token), len(payload), payload)
        return msg

    def __unicode__(self):
        return u'APNService %s' % self.name


class Notification(models.Model):
    service = models.ForeignKey(APNService)
    message = models.CharField(max_length=200)
    badge = models.PositiveIntegerField(default=1, null=True)
    sound = models.CharField(max_length=30, null=True, default='default')
    created_at = models.DateTimeField(auto_now_add=True)
    last_sent_at = models.DateTimeField(null=True, blank=True)

    def push__all_devices(self):
        self.service.push_notification_to_devices(self)

    def __unicode__(self):
        return u'Notification: %s' % self.message

    @staticmethod
    def is_valid_length(message, badge=None, sound=None):
        aps = {'alert': message}
        if badge is not None:
            aps['badge'] = badge
        if sound is not None:
            aps['sound'] = sound
        message = {'aps': aps}
        payload = json.dumps(message, separators=(',', ':'))
        return len(payload) <= 256


class Device(models.Model):
    """
    Represents an iOS device with unique token.
    """
    PLATFORM_CHOICES = (('iPhone', 'iPhone'), ('iPad', 'iPad'), ('iPod', 'iPod'))

    token = models.CharField(max_length=64, blank=False, null=False)
    is_active = models.BooleanField(default=True)
    service = models.ForeignKey(APNService)
    users = models.ManyToManyField(User, null=True, blank=True, related_name='ios_devices')
    added_at = models.DateTimeField(auto_now_add=True)
    last_notified_at = models.DateTimeField(null=True, blank=True)
    platform = models.CharField(max_length=30, blank=True, null=True, choices=PLATFORM_CHOICES)
    display = models.CharField(max_length=30, blank=True, null=True)

    def push_notification(self, notification):
        """
        Pushes a ios_notifications.models.Notification instance to an the device.
        For more details see http://developer.apple.com/library/mac/#documentation/NetworkingInternet/Conceptual/RemoteNotificationsPG/ApplePushService/ApplePushService.html
        """
        if not isinstance(notification, Notification):
            raise TypeError('notification should be an instance of ios_notifications.models.Notification')

        notification.service.push_notification_to_devices(notification, [self])
        self.save()

    def __unicode__(self):
        return u'Device %s' % self.token

    class Meta:
        unique_together = ('token', 'service')


class FeedbackService(BaseService):
    """
    The service provided by Apple to inform you of devices which no longer have your app installed
    and to which notifications have failed a number of times. Use this class to check the feedback
    service and deactivate any devices it informs you about.

    https://developer.apple.com/library/ios/#documentation/NetworkingInternet/Conceptual/RemoteNotificationsPG/CommunicatingWIthAPS/CommunicatingWIthAPS.html#//apple_ref/doc/uid/TP40008194-CH101-SW3
    """
    apn_service = models.ForeignKey(APNService)

    PORT = 2196

    fmt = '!lh32s'

    def connect(self):
        return super(FeedbackService, self).connect(self.apn_service.certificate, self.apn_service.private_key)

    def call(self):
        """
        Calls the Feedback service
        """
        if self.connect():
            device_tokens = []
            try:
                while True:
                    data = self.connection.recv(38)  # 38 being the length in bytes of the binary format feedback tuple.
                    timestamp, token_length, token = struct.unpack(self.fmt, data)
                    device_token = hexlify(token)
                    device_tokens.append(device_token)
            except OpenSSL.SSL.ZeroReturnError:
                # Nothing to receive
                pass
            devices = Device.objects.filter(token__in=device_tokens, service=self.apn_service)
            devices.update(is_active=False)
            self.disconnect()
            return devices.count()

    def __unicode__(self):
        return u'FeedbackService %s' % self.name