import os
import json
import inspect
import typing

import numpy as np
from qtpy.QtCore import QProcess, QSharedMemory
import serf.tools.features as features
from serf.tools.features.base import FeatureBase
from serf.tools.utils.misc_functions import *
from serf.tools.utils._shared import singleton
import serf.scripts

# django app management
from django.forms.models import model_to_dict
from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone
import django


# call the django setup routine
from serf.boot_django import boot_django
boot_django()

from serf.models import *


@singleton
class DBWrapper(object):
    def __init__(self):
        self.current_subject = None
        self.current_procedure = None

        self.active_features = []  # list of feature class instances
        self.all_features = dict()
        self.list_all_features()

    # SUBJECT ==========================================================================================================
    def load_or_create_subject(self, subject_details):
        # validate that the subject doesn't already exist
        if subject_details["id"] == "":
            print("You must enter a subject id.")
            return -1

        self.current_subject, created = Subject.objects.get_or_create(id=subject_details['id'])

        if created:
            print(f"Created a new Subject with id {self.current_subject.subject_id}")
            # add details
            for key, val in subject_details.items():
                # subject_id is an auto-field
                if hasattr(self.current_subject, key) and key != "subject_id":
                    setattr(self.current_subject, key, val)
            self.current_subject.save()
        else:
            print(f"Using existing Subject with id {self.current_subject.subject_id}.")

        return self.current_subject.subject_id

    # Fetching from DB
    def select_subject(self, sub_id):
        try:
            self.current_subject = Subject.objects.get(subject_id=sub_id)
        except ObjectDoesNotExist:
            self.current_subject = None

    @staticmethod
    def list_all_subjects():
        return Subject.objects.order_by('-subject_id').values_list('id', flat=True)

    @staticmethod
    def load_subject_details(_id):
        try:
            return model_to_dict(Subject.objects.get(id=_id))
        except ObjectDoesNotExist:
            return model_to_dict(Subject())

    # PROCEDURE ========================================================================================================
    def load_or_create_procedure(self, procedure_details):
        # validate that the subject doesn't already exist
        if not procedure_details["subject_id"]:
            print("You must enter a subject id.")
            return -1

        # subject field needs to be an instance of Subject(), if not, remove key from dict.
        # We do have the subject id field set.
        if "subject" in procedure_details.keys():
            if not isinstance(procedure_details["subject"], Subject):
                del procedure_details["subject"]

        # Some of the fields cannot be used in get_or_create because they cannot be hashed
        skip_fields = ["a", "e", "entry", "target", "target_name", "type"]

        if "procedure_id" in procedure_details and procedure_details["procedure_id"] is not None:
            self.current_procedure, created = \
                Procedure.objects.get_or_create(procedure_id=procedure_details["procedure_id"])
        else:
            procedure_details.pop("procedure_id")
            filter_kwargs = {k: v for k, v in procedure_details.items() if k not in skip_fields}
            self.current_procedure, created = Procedure.objects.get_or_create(**filter_kwargs)

        if created:
            print(f"Created new Procedure with id {self.current_procedure.procedure_id}.")
            # add the skip_fields to the created procedure
            for k, v in procedure_details.items():
                if k in skip_fields:
                    if hasattr(self.current_procedure, k):
                        setattr(self.current_procedure, k, v)
            self.current_procedure.save()
        else:
            print(f"Loading existing Procedure with id {self.current_procedure.procedure_id}.")

        return self.current_procedure.procedure_id

    def select_procedure(self, proc_id):
        try:
            self.current_procedure = Procedure.objects.get(procedure_id=proc_id)
        except ObjectDoesNotExist:
            self.current_procedure = None

    @staticmethod
    def list_all_procedures(s_id):
        return Procedure.objects.filter(subject=s_id).order_by('date')

    @staticmethod
    def load_procedure_details(p_id: typing.Optional[int], fields=None, exclude=None) -> typing.Dict:
        if p_id is not None:
            try:
                curr_proc = Procedure.objects.get(procedure_id=p_id)
            except ObjectDoesNotExist:
                curr_proc = Procedure()
        else:
            curr_proc = Procedure()

        return model_to_dict(curr_proc, fields=fields, exclude=exclude)

    # DATUM ============================================================================================================
    # return datum ids for values greater than gt
    # matches current procedure
    def list_all_datum_ids(self, gt=0):
        return Datum.objects.filter(procedure=self.current_procedure,
                                    span_type='period',
                                    datum_id__gt=gt).order_by('number').values_list('datum_id', flat=True)

    # Get the list of channels for the most recent non-empty datum in this procedure
    def list_channel_labels(self) -> list:
        labels = []
        for dat in self.list_all_datum_ids():
            try:
                store: DatumStore = Datum.objects.get(datum_id=dat).store
                if store.n_channels > 0:
                    labels = list(store.channel_labels)
            except ObjectDoesNotExist:
                continue
        return labels

    def load_depth_data(self, chan_lbl='None', gt=0, do_hp=True, return_uV=True):
        all_data = self.list_all_datum_ids(gt=gt)

        depth_detail_id = DetailType.objects.filter(name='depth').values_list('detail_type_id', flat=True)

        out_info = dict()
        for d_id in all_data:
            try:
                datum = Datum.objects.get(datum_id=d_id)
                ddv = datum._detail_values.get(detail_type_id=depth_detail_id[0])
                if ddv.value:
                    depth_value = float(ddv.value)
                else:
                    continue

                if chan_lbl.lower() == "all":
                    chan_id = np.s_[:]
                else:
                    chan_id = datum.store.channel_labels.index(chan_lbl)
                if chan_id != -1:
                    chan_data = datum.store.get_data()[chan_id, :]
                else:
                    chan_data = np.nan((datum.store.get_data().shape[0],))

                if return_uV:
                    chan_data = int_to_voltage(chan_data)

                if do_hp:
                    chan_data = high_pass_filter(chan_data)

                out_info[datum.datum_id] = [depth_value, chan_data, datum.is_good[chan_id]]

            except (ObjectDoesNotExist, IndexError) as e:
                continue

        return out_info

    # Saving to DB
    def save_depth_datum(self, depth=0.000, data=None, is_good=np.array([True], dtype=bool), group_info=None,
                         start_time=django.utils.timezone.now(), stop_time=django.utils.timezone.now()):
        if self.current_procedure:
            # check if depth already recorded. If so, we need to delete it to make sure the new features are computed
            # and that the updated value gets picked up by the plots.
            dt, _ = DetailType.objects.get_or_create(name="depth")
            ddv = DatumDetailValue.objects.filter(datum__in=Datum.objects.filter(procedure=self.current_procedure),
                                                  detail_type=dt,
                                                  value=depth)
            if ddv:
                # this will cascade to all dependent fields like ddv
                print(f"Deleting duplicate depth data for procedure {self.current_procedure}, depth {depth}")
                Datum.objects.get(datum_id=ddv[0].datum_id).delete()

            # create new datum
            dat = Datum()
            dat.procedure = self.current_procedure
            dat.is_good = is_good
            dat.span_type = 'period'
            if not timezone.is_aware(start_time):
                start_time = start_time.replace(tzinfo=timezone.utc)
            dat.start_time = start_time
            if not timezone.is_aware(stop_time):
                stop_time = stop_time.replace(tzinfo=timezone.utc)
            dat.stop_time = stop_time
            dat.save()

            # add datum detail values
            dat.update_ddv('depth', depth)
            print(f"Saved new datum entry for procedure {dat.procedure} and depth {depth} with id {dat.datum_id}")

            ds = DatumStore()
            ds.datum = dat

            # channel labels needs to be a list of strings
            if type(group_info) is dict:
                ds.channel_labels = [x['label'] for x in group_info]
            elif type(group_info) is list:
                if type(group_info[0]) is dict:
                    ds.channel_labels = [x['label'] for x in group_info]
                else:
                    ds.channel_labels = group_info

            ds.set_data(data)
            ds.save()
            print(f"Saved new DatumStore for datum id {ds.datum.datum_id} with channel labels {ds.channel_labels}")

    # FEATURES =========================================================================================================
    def list_all_features(self):  # lists from files in the features directory and create the DB entry if needed
        # dictionary {category: [list of tuple (class name, class)]}
        # list all the modules in features
        modules = inspect.getmembers(features, inspect.ismodule)
        for mod_name, mod in modules:
            for cla in inspect.getmembers(mod, inspect.isclass):
                if issubclass(cla[1], FeatureBase) and cla[1] != FeatureBase:
                    if cla[1].category not in self.all_features.keys():
                        self.all_features[cla[1].category] = []

                    # check if already exist in DB
                    db_feature, created = FeatureType.objects.get_or_create(name=cla[1].name)
                    if created:
                        db_feature.description = cla[1].desc
                        db_feature.save()
                    self.all_features[cla[1].category].append(cla[1](db_feature.feature_type_id))

    # to_select is a list of all the categories to process
    def select_features(self, to_select):
        # list of feature categories
        for select in to_select:
            # dict: {category: [(feature name, feature class),]}
            if select in self.all_features.keys():
                self.active_features.extend(self.all_features[select])

    def check_and_compute_features(self, datum_id):
        output = False

        try:
            datum = Datum.objects.get(datum_id=datum_id)

            for feat in self.active_features:
                # check if already computed, featuretype gets created when listing all available features
                feature_type = FeatureType.objects.get(feature_type_id=feat.db_id)

                data_feature_value = datum._feature_values.filter(feature_type=feature_type)

                if len(data_feature_value) == 0:  # does not exist
                    # compute data
                    value, x_vec = feat.run(datum.store.get_data())

                    new_data_feature_value = DatumFeatureValue(datum=datum,
                                                               feature_type=feature_type)
                    new_data_feature_value.save()

                    dfs = DatumFeatureStore(
                        dfv=new_data_feature_value,
                        # datum_store keeps channel labels as a ', ' separated string. Need to convert back to a
                        # list. If not, all characters get comma separated.
                        channel_labels=datum.store.channel_labels,
                        n_channels=datum.store.n_channels,
                        n_features=1,
                        x_vec=x_vec
                    )
                    dfs.set_data(value)

                output = True
        except ObjectDoesNotExist:
            output = False
        return output

    def load_features_data(self, category='DBS', chan_lbl='None', gt=0):
        if category in self.all_features.keys():
            features = self.all_features[category]

            all_data = self.list_all_datum_ids(gt=gt)
            detail_type_id = DetailType.objects.filter(name='depth').values_list('detail_type_id', flat=True)

            out_info = dict()
            for id in all_data:
                try:
                    datum = Datum.objects.get(datum_id=id)

                    tmp_out = dict()
                    depth_str = datum._detail_values.filter(detail_type_id=detail_type_id[0]).\
                        values_list('value', flat=True)

                    if len(depth_str) > 0:
                        tmp_out['depth'] = float(depth_str[0])
                    else:
                        continue

                    for feat in features:
                        feat_type = FeatureType.objects.filter(name=feat.name).values_list('feature_type_id', flat=True)
                        feat_value = datum._feature_values.filter(feature_type_id=feat_type[0])

                        if chan_lbl.lower() == "all":
                            chan_id = np.s_[:]
                        else:
                            chan_id = feat_value[0].store.channel_labels.index(chan_lbl)

                        if chan_id != -1:
                            tmp_out[feat.name] = [feat_value[0].store.x_vec,
                                                  feat_value[0].store.get_data()[chan_id],
                                                  datum.is_good[chan_id]]

                    # check if all features are present, if not stop here to avoid having data missing features
                    if all(x.name in tmp_out.keys() for x in features):
                        out_info[datum.datum_id] = dict(tmp_out)
                    else:
                        continue
                except (ObjectDoesNotExist, IndexError, TypeError) as e:
                    continue

            return out_info

    @staticmethod
    def return_enums(model_name):

        if model_name.lower() == 'subject':
            fields = Subject._meta.get_fields()
        elif model_name.lower() == 'procedure':
            fields = Procedure._meta.get_fields()
        else:
            fields = None

        out_dict = dict()
        for x in fields:
            if type(x) == EnumField:
                out_dict[x.attname] = [choice[1] for choice in x.choices]

        return out_dict


class ProcessWrapper:
    def __init__(self, process_name):

        # define process
        self.process_name = process_name

        self.worker = QProcess()
        self.worker.setProcessChannelMode(QProcess.ForwardedChannels)

        # The shared memory object will be used the send the process parameters and the kill signal (the last byte).
        # The first byte is the status.
        # The next 9 bytes are reserved.
        # Bytes 10-4086 are space-separated string of parameters.
        # The final byte (4096) is a kill signal: 1 to terminate; 0 to keep going.
        self.shared_memory = QSharedMemory()
        self.shared_memory.setKey(self.process_name)

        self.manage_shared_memory()

    def manage_shared_memory(self):
        # before starting the worker, we will check whether the named shared memory object exists and
        # as a failsafe, we will send the kill signal to all attached processes.
        if self.shared_memory.attach() or self.shared_memory.isAttached():
            # if attached means that the shared memory block already exists, so terminate process.
            #  The last byte is a kill signal.
            self.shared_memory.lock()
            self.shared_memory.data()[-1:] = memoryview(b"\x01")
            self.shared_memory.unlock()
        # if shared memory is not attached and can't attach (i.e. doesn't exits) create it
        elif not self.shared_memory.attach() and not self.shared_memory.isAttached():
            # going to be 4096 regardless of value, as long as < 4096
            self.shared_memory.create(4096)

    def send_settings(self, settings):
        """
        Send settings to running process via QSharedMemory.
        :param settings:
            dictionary of settings e.g.: {'subject_id': int, 'features':['DBS', 'LFP']}
        :return:
            None
        """
        # offset the bytes by 10 to avoid the last one (i.e. kill signal)
        sett_str = json.dumps(settings)
        # convert settings string into bytes
        b_settings = sett_str.encode('utf-8')
        len_b_settings = len(b_settings)

        if self.shared_memory.isAttached() and len_b_settings < 4086:
            self.shared_memory.lock()
            # clear to make sure we don't have leftovers
            self.shared_memory.data()[10:-10] = np.zeros((self.shared_memory.size() - 20,), dtype=np.int8).tobytes()
            self.shared_memory.data()[10:len_b_settings+10] = b_settings
            self.shared_memory.unlock()

    def start_worker(self):
        # start process
        if self.process_name != '':

            # make sure kill signal is off
            self.shared_memory.lock()
            self.shared_memory.data()[-1:] = memoryview(b"\x00")
            self.shared_memory.unlock()

            run_command = "python " + os.path.join(os.path.dirname(serf.scripts.__file__),
                                                   self.process_name + ".py ")

            self.worker.start(run_command)

    # function to properly terminate QProcess
    def kill_worker(self):
        if self.shared_memory.isAttached():
            self.shared_memory.lock()
            self.shared_memory.data()[-1:] = memoryview(b"\x01")
            self.shared_memory.unlock()
        else:
            self.worker.kill()

        self.worker.waitForFinished()
        # detach shared memory to destroy
        self.shared_memory.detach()

    def worker_status(self):
        # The status is in the first byte.
        if self.shared_memory.isAttached():
            self.shared_memory.lock()
            out = np.frombuffer(self.shared_memory.data()[:1], dtype=np.int8)[0]
            self.shared_memory.unlock()
        else:
            out = 0

        return out

    def is_running(self):
        _state = self.worker.state()
        return _state == QProcess.ProcessState.Running
