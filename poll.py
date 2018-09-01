#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright 2007 World Wide Workshop Foundation
# Copyright 2007 Collabora Ltd
# Copyright 2008 Morgan Collett
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA
#
# If you find this activity useful or end up using parts of it in one of
# your own creations we would love to hear from you at
# info@WorldWideWorkshop.org !

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('GdkPixbuf', '2.0')
gi.require_version('TelepathyGLib', '0.12')
from gi.repository import Gtk
from gi.repository import GdkPixbuf
from gi.repository import TelepathyGLib

import subprocess
import cPickle
import json
import logging
import tempfile
import dbus

from hashlib import sha1
from datetime import date
from gettext import gettext as _

from sugar3.presence.tubeconn import TubeConnection

from sugar3.activity import activity
from sugar3.graphics.alert import Alert, NotifyAlert, ConfirmationAlert
from sugar3.graphics.icon import Icon

from sugar3.presence import presenceservice
from sugar3.datastore import datastore
from sugar3 import profile

from Widgets import Toolbar

# Interfaces
from Widgets import NewPollCanvas   # Create a new poll.
from Widgets import SelectCanvas    # Select one available poll.
from Widgets import PollCanvas      # Participate in a poll.

from PollSession import PollSession
from PollSession import Poll
import emptypanel
from graphics import CHART_TYPE_PIE, CHART_TYPE_VERTICAL_BARS

SERVICE = "org.worldwideworkshop.olpc.PollBuilder"
IFACE = SERVICE
PATH = "/org/worldwideworkshop/olpc/PollBuilder"


class PollBuilder(activity.Activity):
    """
    Sugar activity for polls

    Poll implements a simple tool that allows children to express
    their opinions on a given topic by selecting one of five
    answer choices and submitting a vote. The results are tallied
    by total number of votes and percentage of total votes cast.

    A future version of this activity will be networked over the
    OLPC mesh to allow sharing of the poll.
    """

    def __init__(self, handle):

        activity.Activity.__init__(self, handle)

        self._logger = logging.getLogger('poll-activity')
        self._logger.debug('Starting Poll activity')

        self._polls = set()
        self.current_vote = None

        # This property allows result viewing while voting
        self._view_answer = True

        # This property allows use image in answer
        self._use_image = False

        # This property allows play a sound when click in
        # the button to make a vote
        self._play_vote_sound = False

        # This property allows remember in the radio button options
        # the last vote
        self._remember_last_vote = True

        # This property has the image size
        self._image_size = {'height': 100, 'width': 100}

        # the active poll
        self._poll = None

        # get the Presence Service
        self.pservice = presenceservice.get_instance()
        self.initiating = False

        # Buddy object for you
        owner = self.pservice.get_owner()
        self.owner = owner
        self.nick = owner.props.nick
        self.nick_sha1 = sha1(self.nick).hexdigest()

        toolbar = Toolbar(self)
        toolbar.create_button.connect('clicked', self.__button_new_clicked)
        toolbar.choose_button.connect('clicked', self.__button_select_clicked)
        toolbar.export_data_bt.connect('clicked', self.__save_data_cb)
        toolbar.export_image_bt.connect('clicked', self.__save_image_cb)

        toolbar.pie_chart_button.connect(
            'clicked', self.__chart_type_clicked_cb, CHART_TYPE_PIE)
        toolbar.vbar_chart_button.connect(
            'clicked', self.__chart_type_clicked_cb, CHART_TYPE_VERTICAL_BARS)
        self._chart_type_selected = CHART_TYPE_PIE

        self.set_toolbar_box(toolbar)

        emptypanel.show(self, 'new-poll', _('Create a new poll'),
                        _('Create'), self.__button_new_clicked)

        self.show_all()

        self.poll_session = None

        self.connect('shared', self.__shared_cb)
        self.connect('joined', self.__joined_cb)

    def __create_pixbufs(self, images_ds_object_id):
        """
        Crea las imágenes de la encuesta, al leer desde el journal.
        """

        pixbufs = {}

        for index, ds_object_id in images_ds_object_id.iteritems():
            if not ds_object_id == '':
                image_file_path = datastore.get(ds_object_id).file_path
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(
                    image_file_path, self._image_size['height'],
                    self._image_size['width'])
                pixbufs[int(index)] = pixbuf

            else:
                pixbufs[int(index)] = ''

        return pixbufs

    def __get_images_ds_objects(self, images_ds_object_id):
        """
        Obtiene las imagenes almacenadas en el jornal.
        """

        images_ds_objects = {}

        for index, ds_object_id in images_ds_object_id.iteritems():
            images_ds_objects[int(index)] = {}

            if not ds_object_id == '':
                images_ds_objects[int(index)]['id'] = ds_object_id
                images_ds_objects[int(index)]['file_path'] = \
                    datastore.get(ds_object_id).file_path

        return images_ds_objects

    def read_file(self, file_path):
        try:
            with open(file_path, 'r') as f:
                data = json.load(f)

                self._view_answer = data['view_answer']
                self._remember_last_vote = data['remember_last_vote']
                self._play_vote_sound = data['play_vote_sound']
                self._use_image = data['use_image']
                self._image_size = data['image_size']
                self._polls = set()
                for poll_data in data['polls_data']:
                    # json stores the dictionary keys as strings,
                    # convert to int
                    options = {}
                    for key in poll_data['options']:
                        options[int(key)] = poll_data['options'][key]

                    images_ds_objects = {}
                    for key in poll_data['images_ds_objects']:
                        images_ds_objects[int(key)] = \
                            poll_data['images_ds_objects'][key]

                    data = {}
                    for key in poll_data['data']:
                        data[int(key)] = poll_data['data'][key]

                    images = self.__create_pixbufs(images_ds_objects)
                    images_ds_object = self.__get_images_ds_objects(
                        images_ds_objects)
                    self._polls.add(Poll(
                        self, poll_data['title'], poll_data['author'],
                        poll_data['active'],
                        date.fromordinal(poll_data['createdate']),
                        poll_data['maxvoters'], poll_data['question'],
                        poll_data['number_of_options'],
                        options, data, poll_data['votes'],
                        images, images_ds_object))
        except:
            # if can't read json, try read with the old format
            self._old_read_file(file_path)

        # if there are polls loaded, show the selection screen
        # if not, show the creation screen
        if self._polls:
            self.set_canvas(SelectCanvas(self))
        else:
            emptypanel.show(self, 'new-poll', _('Create a new poll'),
                            _('Create'), self.__button_new_clicked)

        self.get_toolbar_box().update_configs()

    def _old_read_file(self, file_path):
        """
        This method use pickle to read files saved by old versions of the
        activity
        """

        self._logger.debug(
            'Reading OLD FORMAT file from datastore via Journal: %s' %
            file_path)

        self._polls = set()

        f = open(file_path, 'r')
        num_polls = cPickle.load(f)
        activity_settings = cPickle.load(f)
        self._view_answer = activity_settings['view_answer']
        self._remember_last_vote = activity_settings['remember_last_vote']
        self._play_vote_sound = activity_settings['play_vote_sound']
        self._use_image = activity_settings['use_image']
        self._image_size = cPickle.load(f)

        for p in range(num_polls):
            title = cPickle.load(f)
            author = cPickle.load(f)
            active = cPickle.load(f)
            createdate_i = cPickle.load(f)
            maxvoters = cPickle.load(f)
            question = cPickle.load(f)
            number_of_options = cPickle.load(f)
            options = cPickle.load(f)
            data = cPickle.load(f)
            votes = cPickle.load(f)
            images_ds_objects_id = cPickle.load(f)
            images = self.__create_pixbufs(images_ds_objects_id)

            images_ds_object = self.__get_images_ds_objects(
                images_ds_objects_id)

            poll = Poll(self, title, author, active,
                        date.fromordinal(int(createdate_i)),
                        maxvoters, question, number_of_options, options,
                        data, votes, images, images_ds_object)

            self._polls.add(poll)

        f.close()

        self.set_canvas(SelectCanvas(self))

    def write_file(self, file_path):
        """
        Implement writing to the journal

        This is called within sugar3.activity.Activity code
        which provides the file_path.
        """

        polls_data = []
        for poll in self._polls:
            polls_data.append(poll.dump())
        data = {
            'view_answer': self._view_answer,
            'remember_last_vote': self._remember_last_vote,
            'play_vote_sound': self._play_vote_sound,
            'use_image': self._use_image,
            'image_size': self._image_size,
            'polls_data': polls_data}

        with open(file_path, 'w') as f:
            f.write(json.dumps(data))

    def get_alert(self, title, text):
        """
        Show an alert above the activity.
        """

        alert = NotifyAlert(timeout=5)
        alert.props.title = title
        alert.props.msg = text
        self.add_alert(alert)
        alert.connect('response', self.__alert_cancel_cb)
        alert.show()

    def __alert_cancel_cb(self, alert, response_id):
        """
        Callback for alert events
        """

        self.remove_alert(alert)

    def set_canvas(self, widget):
        # change the toolbar bassed on the canvas selected
        enable_charts = type(widget) is PollCanvas
        toolbarbox = self.get_toolbar_box()

        toolbarbox.pie_chart_button.set_sensitive(enable_charts)
        toolbarbox.vbar_chart_button.set_sensitive(enable_charts)
        toolbarbox.export_data_bt.set_sensitive(enable_charts)
        toolbarbox.export_image_bt.set_sensitive(enable_charts)

        activity.Activity.set_canvas(self, widget)

    def _poll_canvas(self):
        """
        Show the poll canvas where children vote on an existing poll.
        """
        self.current_vote = None
        return PollCanvas(self._poll, self.current_vote,
                          self._view_answer, self._chart_type_selected)

    def _select_poll_button_cb(self, button, event, sha=None):
        """
        A VOTE or SEE RESULTS button was clicked.
        """
        if not sha:
            self._logger.debug('Strange, which button was clicked?')
            return

        self.__switch_to_poll(sha)
        self.set_canvas(self._poll_canvas())

    def _delete_poll_button_cb(self, button, event, sha, title):
        """
        A DELETE button was clicked.
        """

        alert = ConfirmationAlert()
        alert.props.title = _('Delete Poll?')
        alert.props.msg = _('Do you want delete the poll "%s"?') % title
        alert.connect('response', self._delete_alert_confirmation_cb, sha)
        self.add_alert(alert)

    def _delete_alert_confirmation_cb(self, alert, response_id, sha):
        self.remove_alert(alert)

        if response_id is Gtk.ResponseType.OK:
            if not sha:
                self._logger.debug('Strange, which button was clicked?')
                return

            for poll in self._polls:
                if poll.sha == sha:
                    self._polls.remove(poll)
                    break

            self.set_canvas(SelectCanvas(self))

    def __play_vote_button_sound(self):

        try:
            # FIXME: Cambiar por gst
            subprocess.Popen("aplay extras/vote-sound.wav", shell=True)

        except (OSError, ValueError), e:
            logging.exception(e)

    def poll_vote(self, vote):
        """
        Register a vote

        Take the selected option from self.current_vote
        and increment the poll_data.
        """

        self.current_vote = vote

        if self._poll.vote_count >= self._poll.maxvoters:
            self._logger.debug('Hit the max voters, ignoring this vote.')
            return

        self._logger.debug('Voted ' + str(self.current_vote))

        try:
            self._poll.register_vote(self.current_vote, self.nick_sha1)

        except OverflowError:
            self._logger.debug('Local vote failed: '
                               'maximum votes already registered.')

        except ValueError:
            self._logger.debug('Local vote failed: poll closed.')

        self._logger.debug('Results: ' + str(self._poll.data))

        if self._play_vote_sound:
            self.__play_vote_button_sound()

        if not self._remember_last_vote:
            self.current_vote = None

        self.set_canvas(PollCanvas(
            self._poll, self.current_vote,
            self._view_answer, self._chart_type_selected))

    def __button_select_clicked(self, button):
        """
        Show Choose a Poll canvas
        """

        self.set_canvas(SelectCanvas(self))

    def __button_new_clicked(self, button):
        """
        Show Build a Poll canvas.
        """
        self._create_new_poll()

    def _create_new_poll(self):
        # Reset vote data to 0
        self._poll = Poll(activity=self, author=profile.get_nick_name(),
                          active=False)
        self.current_vote = None
        self.set_canvas(NewPollCanvas(self._poll))

    def button_edit_clicked(self, button):

        self.set_canvas(NewPollCanvas(self._poll))

    def reset_poll(self):
        self._poll = None

    def __switch_to_poll(self, sha):
        """
        Set self._poll to the specified poll with sha

        sha -- string
        """

        for poll in self._polls:
            if poll.sha == sha:
                self._poll = poll
                break

    def get_my_polls(self):
        """
        Return list of Polls for all polls I created.
        """

        return [poll for poll in self._polls if poll.author == self.nick]

    def set_view_answer(self, view_answer):
        self._view_answer = view_answer
        if type(self.get_canvas()) is PollCanvas and self._poll.active:
            poll_canvas = self.get_canvas()
            poll_canvas.set_view_answer(view_answer)

    def get_view_answer(self):
        return self._view_answer

    def set_use_image(self, use_image):
        self._use_image = use_image
        if type(self.get_canvas()) is NewPollCanvas:
            new_poll_canvas = self.get_canvas()
            new_poll_canvas.set_image_widgets_visible(use_image)

    def get_use_image(self):
        return self._use_image

    def set_remember_last_vote(self, remember_last_vote):
        self._remember_last_vote = remember_last_vote
        if type(self.get_canvas()) is PollCanvas:
            if self._remember_last_vote:
                self.current_vote = self._poll.last_vote
            else:
                self.current_vote = None
            self.set_canvas(PollCanvas(
                self._poll, self.current_vote,
                self._view_answer, self._chart_type_selected))

    def get_remember_last_vote(self):
        return self._remember_last_vote

    def __save_image_cb(self, button):
        if type(self.get_canvas()) is not PollCanvas:
            return
        chart = self.get_canvas().chart
        image_file = tempfile.NamedTemporaryFile(mode='w+b', suffix='.png')
        journal_entry = datastore.create()
        journal_entry.metadata['title'] = \
            '"%s" results chart' % self._poll.title
        journal_entry.metadata['keep'] = '0'
        journal_entry.metadata['mime_type'] = 'image/png'

        # generate the image
        preview_str = chart.save_image(image_file.file, 800, 600)
        image_file.file.close()
        journal_entry.file_path = image_file.name

        journal_entry.metadata['preview'] = dbus.ByteArray(
            preview_str.getvalue())

        logging.debug('Create %s image file', image_file.name)
        datastore.write(journal_entry)
        self._show_journal_alert(
            _('Chart created'), _('Open in the Journal'),
            journal_entry.object_id)

    def _show_journal_alert(self, title, msg, object_id):
        open_alert = Alert()
        open_alert.props.title = title
        open_alert.props.msg = msg
        open_icon = Icon(icon_name='zoom-activity')
        open_alert.add_button(Gtk.ResponseType.APPLY,
                              _('Show in Journal'), open_icon)
        open_icon.show()
        ok_icon = Icon(icon_name='dialog-ok')
        open_alert.add_button(Gtk.ResponseType.OK, _('Ok'), ok_icon)
        ok_icon.show()
        # Remove other alerts
        for alert in self._alerts:
            self.remove_alert(alert)

        self.add_alert(open_alert)
        open_alert.connect('response', self.__open_response_cb, object_id)
        open_alert.show()

    def __open_response_cb(self, alert, response_id, object_id):
        if response_id is Gtk.ResponseType.APPLY:
            activity.show_object_in_journal(object_id)
        self.remove_alert(alert)

    def __save_data_cb(self, button):
        if type(self.get_canvas()) is not PollCanvas:
            return

        chart_params = {}
        axis = {'tickFont': 'Sans', 'labelFont': 'Sans', 'labelFontSize': 14,
                'labelColor': '#666666', 'lineColor': '#b3b3b3',
                'tickColor': '#000000', 'tickFontSize': 12}
        chart_params['font_options'] = {
            'titleFont': 'Sans', 'titleFontSize': 12, 'titleColor': '#000000',
            'axis': axis}

        title = _('Results from poll "%s"') % self._poll.title
        chart_params['title'] = title
        chart_params['x_label'] = ''
        chart_params['y_label'] = ''
        chart_params['current_chart.type'] = 1
        xo_color = profile.get_color()
        chart_params['chart_line_color'] = xo_color.get_stroke_color()
        chart_params['chart_color'] = xo_color.get_fill_color()

        """
        'chart_data': [
            ['hello', 200.0],
            ['mrch', 100.0]],
        """
        data = []

        for choice in range(self._poll.number_of_options):
            # data is used by the chart
            data.append([self._poll.options[choice], self._poll.data[choice]])

        chart_params['chart_data'] = data

        logging.debug('chart_data %s', chart_params)

        # save to the journal
        data_file = tempfile.NamedTemporaryFile(mode='w+b', suffix='.json')
        journal_entry = datastore.create()
        journal_entry.metadata['title'] = title
        journal_entry.metadata['keep'] = '0'
        journal_entry.metadata['mime_type'] = 'application/x-chart-activity'
        journal_entry.metadata['activity'] = 'org.sugarlabs.SimpleGraph'

        json.dump(chart_params, data_file.file)
        data_file.file.close()
        journal_entry.file_path = data_file.name

        logging.debug('Create %s data file', data_file.name)
        datastore.write(journal_entry)
        self._show_journal_alert(
            _('Exported data'), _('Open in the Journal'),
            journal_entry.object_id)

    def __chart_type_clicked_cb(self, button, chart_type):
        self._chart_type_selected = chart_type
        if type(self.get_canvas()) is PollCanvas:
            self.get_canvas().chart.set_chart_type(chart_type)

    def vote_on_poll(self, author, title, choice, votersha):
        """
        Register a vote on a poll from the mesh.

        author -- string
        title -- string
        choice -- integer 0-4
        votersha -- string
          sha1 of the voter nick
        """

        for poll in self._polls:
            if poll.author == author and poll.title == title:
                try:
                    poll.register_vote(choice, votersha)
                    self.get_alert(_('Vote'),
                                   _('Somebody voted on %s') % title)

                    if self._poll == poll and \
                            type(self.get_canvas()) is PollCanvas:
                        self.set_canvas(PollCanvas(self._poll,
                                                   self.current_vote,
                                                   self._view_answer,
                                                   self._chart_type_selected))

                except OverflowError:
                    self._logger.debug(
                        'Ignored mesh vote %u from %s:'
                        ' poll reached maximum votes.',
                        choice, votersha)

                except ValueError:
                    self._logger.debug(
                        'Ignored mesh vote %u from %s: poll closed.',
                        choice, votersha)

    # COLABORATION >>

    def __shared_cb(self, activity):
        """
        Callback for completion of sharing this activity.
        """

        self._logger.debug('My activity was shared')
        self.initiating = True

        self.__sharing_setup()

        self._logger.debug('This is my activity: making a tube...')

        self.tubes_chan[TelepathyGLib.IFACE_CHANNEL_TYPE_TUBES].OfferDBusTube(
            SERVICE, {})

    def __sharing_setup(self):
        """
        Setup my Tubes channel.

        Called from _shared_cb or _joined_cb.
        """

        if self.shared_activity is None:
            self._logger.error('Failed to share or join activity')
            return

        self.conn = self.shared_activity.telepathy_conn
        self.tubes_chan = self.shared_activity.telepathy_tubes_chan
        self.text_chan = self.shared_activity.telepathy_text_chan

        self.tubes_chan[TelepathyGLib.IFACE_CHANNEL_TYPE_TUBES].connect_to_signal(
            'NewTube', self.__new_tube_cb)

        self.shared_activity.connect('buddy-joined', self.__buddy_joined_cb)
        self.shared_activity.connect('buddy-left', self.__buddy_left_cb)

    def __list_tubes_reply_cb(self, tubes):

        for tube_info in tubes:
            self.__new_tube_cb(*tube_info)

    def __list_tubes_error_cb(self, e):
        self._logger.error('ListTubes() failed: %s', e)

    def __joined_cb(self, activity):
        """
        Callback for completion of joining the activity.
        """

        if not self.shared_activity:
            return

        self._logger.debug('Joined an existing shared activity')
        self.get_alert(_('Joined'), "")

        self.initiating = False
        self.__sharing_setup()

        self._logger.debug('This is not my activity: waiting for a tube...')
        self.tubes_chan[TelepathyGLib.IFACE_CHANNEL_TYPE_TUBES].ListTubes(
            reply_handler=self.__list_tubes_reply_cb,
            error_handler=self.__list_tubes_error_cb)

    def __new_tube_cb(self, id, initiator, type, service, params, state):
        """
        Callback for when we have a Tube.
        """

        self._logger.debug(
            'New tube: ID=%d initator=%d type=%d srv=%s params=%r state=%d',
            id, initiator, type, service, params, state)

        if (type == TelepathyGLib.TubeType.DBUS and service == SERVICE):
            if state == TelepathyGLib.TubeState.LOCAL_PENDING:
                self.tubes_chan[TelepathyGLib.IFACE_CHANNEL_TYPE_TUBES].AcceptDBusTube(
                    id)

            tube_conn = TubeConnection(
                self.conn,
                self.tubes_chan[TelepathyGLib.IFACE_CHANNEL_TYPE_TUBES],
                id,
                group_iface=self.text_chan[TelepathyGLib.IFACE_CHANNEL_INTERFACE_GROUP])

            self.poll_session = PollSession(tube_conn, self.initiating,
                                            self.__get_buddy, self)

    def __buddy_joined_cb(self, activity, buddy):

        self.get_alert(buddy.props.nick, _('Joined'))
        self._logger.debug('Buddy %s joined' % buddy.props.nick)

    def __buddy_left_cb(self, activity, buddy):

        self.get_alert(buddy.props.nick, _('Left'))
        self._logger.debug('Buddy %s left' % buddy.props.nick)

    def __get_buddy(self, cs_handle):
        """
        Get a Buddy from a channel specific handle.
        """

        self._logger.debug('Trying to find owner of handle %u...', cs_handle)
        group = self.text_chan[TelepathyGLib.IFACE_CHANNEL_INTERFACE_GROUP]
        my_csh = group.GetSelfHandle()

        SIGNAL_TELEPATHY = \
            TelepathyGLib.ChannelGroupFlags.CHANNEL_SPECIFIC_HANDLES
        self._logger.debug('My handle in that group is %u', my_csh)

        if my_csh == cs_handle:
            handle = self.conn.GetSelfHandle()
            self._logger.debug('CS handle %u belongs to me, %u', cs_handle,
                               handle)

        elif group.GetGroupFlags() & SIGNAL_TELEPATHY:
            handle = group.GetHandleOwners([cs_handle])[0]
            self._logger.debug('CS handle %u belongs to %u', cs_handle, handle)

        else:
            handle = cs_handle
            self._logger.debug('non-CS handle %u belongs to itself', handle)
            assert handle != 0

        return self.pservice.get_buddy_by_telepathy_handle(
            self.conn.service_name, self.conn.object_path, handle)
        # << COLABORATION
