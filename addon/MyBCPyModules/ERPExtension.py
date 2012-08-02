#===============================================================================
# Passes trigger into a buffer for trigger detection and ERP into another buffer. When a trigger is detected,
# the trial's details and its evoked potential are saved into a MySQL database.
# The application will not be able to exit the response phase until the
# ERP has been collected. Storing of the ERP in the database is asynchronous.
# The app might also show feedback 
#===============================================================================
#===============================================================================
# TODO:
# -Cleanup preflight
# -Cleanup initialize. period_type
# -Process: only trigger goes to one trap. ERPChan go to other trap.
# -Use a thread for database interaction.
#===============================================================================

import Queue
import threading
import numpy as np
#import random
#import time
from EeratAPI.API import *
from MyPythonApps.OnlineAPIExtension import *

class ERPThread(threading.Thread):
    def __init__(self, queue, app):
        threading.Thread.__init__(self)
        self.app = app
        self.queue = queue
        
    def run(self):
        while True:
            try:#Get a message from the queue
                msg = self.queue.get(True, 0.5)
            except:#Queue is empty -> Do the default action.
                self.queue.put({'default': 0})
            else:#We got a message
                key = msg.keys()[0]
                value = msg[key]
                if key=='save_trial':
                    #===========================================================
                    # value is the erp data for this trial.
                    # Everything else can be extracted from app
                    #===========================================================
                    self.app.period.trials.append(Datum(subject_id=self.app.subject.subject_id\
                            , datum_type_id=self.app.period.datum_type_id\
                            , span_type='trial'\
                            , parent_datum_id=self.app.period.datum_id\
                            , IsGood=1, Number=0))#Number=0 kicks a SQL trigger to find the lowest number
                    my_trial=self.app.period.trials[-1]
                    for k in my_trial.detail_values.keys():
                        if k=='dat_Nerve_stim_output': my_trial.detail_values[k]=str(self.app.digistim.intensity)
                        if k=='dat_TMS_powerA': my_trial.detail_values[k]=str(self.app.magstim.intensity)
                        if k=='dat_TMS_powerB': my_trial.detail_values[k]=str(self.app.magstim.intensityb)
                        if k=='dat_TMS_ISI': my_trial.detail_values[k]=str(self.app.magstim.ISI)
                    my_trial.store={'x_vec':self.app.x_vec, 'data':value, 'channel_labels': self.app.chlbs}
                    app.period.EndTime = datetime.datetime.now() + datetime.timedelta(minutes = 5)
                    
                elif key=='default':
                    #===========================================================
                    # TODO: Check the database for the last trial. If it is new, then
                    # retrieve the desired ERP value (ERPFeedbackOf) and then 
                    # change the value of self.app.states['LastERPx100']
                    # Also set last_trial.detail_values['dat_conditioned_result']
                    #===========================================================
                    pass
                self.queue.task_done()#signals to queue job is done. Maybe the stimulator object should do this?

class Template(object):
    params = [
            "PythonApp:ERPDatabase    int        ERPDatabaseEnable= 1 1 0 1 // Enable: 0 no, 1 yes (boolean)",
            "PythonApp:ERPDatabase    int        ERPDatumType= 0 0 0 4 // 0 mep_mapping, 1 mep_io, 2 mep_sici, 3 hr_hunting, 4 hr_io (enumeration)",
            "PythonApp:ERPDatabase    list        TriggerInputChan= 1 Trig % % % // Name of channel used to monitor trigger / control ERP window",
            "PythonApp:ERPDatabase    float        TriggerThreshold= 1 1 0 % // Use this threshold to determine ERP time 0",
            #"PythonApp:ERPDatabase   int            UseSoftwareTrigger= 0 0 0 1  // Use phase change to determine trigger onset (boolean)",
            "PythonApp:ERPDatabase    list        ERPChan= 1 EDC_RAW % % % // Channels to store in database",
            "PythonApp:ERPDatabase    floatlist    ERPWindow= {Start Stop} -500 500 0 % % // Stored window, relative to trigger onset, in millesconds",
            "PythonApp:ERPDatabase    int        ERPFeedbackDisplay= 0 0 0 2 // Feedback as: 0 None, 1 TwoColour, 2 Continuous (enumeration)",
            "PythonApp:ERPDatabase    int        ERPFeedbackOf= 0 0 0 2 // Feedback as: 0 aaa, 1 p2p, 2 residual (enumeration)",
            "PythonApp:ERPDatabase    float        ERPFeedbackThreshold= 3.0 0 % % // (+/-) threshold for correct erp feedback",
        ]
    states = [
            "LastERPx100 10 0 0 0", #Last ERP's interesting value x 100
        ]
    
    @classmethod
    def preflight(cls, app, sigprops):
        if int(app.params['ERPDatabaseEnable'])==1:
            
            #Trigger
            app.trigchan=None
            tch = app.params['TriggerInputChan']
            if len(tch) != 0:
                if False in [isinstance(x, int) for x in tch]:
                    nf = filter(lambda x: not str(x) in chn, tch)
                    if len(nf): raise EndUserError, "TriggerChannel %s not in module's list of input channel names" % str(nf)
                    app.trigchan = [chn.index(str(x)) for x in tch]
                else:
                    nf = [x for x in tch if x < 1 or x > len(chn) or x != round(x)]
                    if len(nf): raise EndUserError, "Illegal TriggerChannel: %s" %str(nf)
                    app.trigchan = [x-1 for x in tch]
            #TODO: Check the trigger threshold
            if app.trigchan:
                trigthresh=app.params['TriggerThreshold'].val
                app.trig_label=tch #This is the channel label.
                app.trigthresh=trigthresh
                
            #ERP channel(s)
            erpch = app.params['ERPChan'].val
            if len(erpch) != 0:
                if False in [isinstance(x, int) for x in erpch]:
                    nf = filter(lambda x: not str(x) in chn, erpch)
                    if len(nf): raise EndUserError, "ERPChan %s not in module's list of input channel names" % str(nf)
                    app.erpchan = [chn.index(str(x)) for x in erpch]
                else:
                    nf = [x for x in erpch if x < 1 or x > len(chn) or x != round(x)]
                    if len(nf): raise EndUserError, "Illegal ERPChan: %s" % str(nf)
                    app.erpchan = [x-1 for x in erpch]
            else:
                raise EndUserError, "Must supply ERPChan"
                
            #ERP window
            erpwin = app.params['ERPWindow'].val
            if len(erpwin)!=2: raise EndUserError, "ERPWindow must have 2 values"
            if erpwin[0]>erpwin[1]: raise EndUserError, "ERPWindow must be in increasing order"
            if erpwin[1]<0: raise EndUserError, "ERPWindow must include up to at least 0 msec after stimulus onset"
            app.erpwin=erpwin
    
    @classmethod
    def initialize(cls, app, indim, outdim):
        if int(app.params['ERPDatabaseEnable'])==1:
            #===================================================================
            # Get our subject from the DB API.
            #===================================================================
            app.subject=get_or_create(Subject, Name=app.params['SubjectName'], species_type='human')
            if not app.subject.subject_type_id:
                my_subject_type=get_or_create(Subject_type, Name='BCPy_healthy')
                app.subject.subject_type_id=my_subject_type.subject_type_id
                
            #===================================================================
            # Get our period from the DB API.
            #===================================================================
            #Determine datum type from parameters.
            period_type_name={0:'mep_mapping', 1:'mep_io', 2:'mep_sici', 3:'hr_hunting', 4:'hr_io'}.get(int(app.params['ERPDatumType']))
            my_period_type=get_or_create(Datum_type, Name=period_type_name)
            app.period = app.subject.get_most_recent_period(datum_type=my_period_type,delay=0)#Will create period if it does not exist.
            
            #===================================================================
            # Prepare the buffers for saving the ERP
            # -leaky_trap contains the ERP (pre_stim_samples + post_stim_samples + some breathing room
            # -trig_trap contains only the trigger channel
            #===================================================================
            app.x_vec=np.arange(app.erpwin[0],app.erpwin[1],1000/app.eegfs,dtype=float)#Needed when saving trials
            app.chlbs = [ch_name[0:-4] if ch_name.endswith('_RAW') else ch_name for ch_name in app.params['ERPChan']]#Needed when saving trials.
            app.post_stim_samples = SigTools.msec2samples(app.erpwin[1], app.eegfs)
            app.pre_stim_samples = SigTools.msec2samples(np.abs(app.erpwin[0]), app.eegfs)
            app.leaky_trap=SigTools.Buffering.trap(app.pre_stim_samples + app.post_stim_samples + 5*spb, len(app.chlbs), leaky=True)
            app.trig_trap = SigTools.Buffering.trap(app.post_stim_samples, 1, trigger_channel=0, trigger_threshold=app.trigthresh)
            
            #===================================================================
            # Use a thread for slower database interactions
            # (saving a trial also calculates all of that trial's features)
            #===================================================================
            app.erp_thread = ERPThread(Queue.Queue(), app)
            app.erp_thread.setDaemon(True) #Dunno, always there in the thread examples.
            app.erp_thread.start() #Starts the thread.
            
            #===================================================================
            # TODO: Setup the ERP feedback elements.
            #===================================================================
        
    @classmethod
    def halt(cls,app):
        if int(app.params['ERPDatabaseEnable'])==1: pass
    
    @classmethod
    def startrun(cls,app):
        if int(app.params['ERPDatabaseEnable'])==1: pass
        
    @classmethod
    def stoprun(cls,app):
        if int(app.params['ERPDatabaseEnable'])==1: pass
    
    @classmethod
    def transition(cls,app,phase):
        if int(app.params['ERPDatabaseEnable'])==1:
            if phase == 'intertrial':
                pass
                
            elif phase == 'baseline':
                pass
            
            elif phase == 'gocue':
                pass
                
            elif phase == 'task':
                pass
                
            elif phase == 'response':
                pass
            
            elif phase == 'stopcue':
                app.erp_collected = False
    
    @classmethod
    def process(cls,app,sig):
        if int(app.params['ERPDatabaseEnable'])==1:
            app.leaky_trap.process(sig[app.erpchan,:])
            app.trig_trap.process(sig[app.trigchan,:])
            
            if app.in_phase('response') and app.trig_trap.full():
                n_excess = (app.trig_trap.nseen-app.trig_trap.sprung_at)-app.trig_trap.nsamples
                app.trig_trap.reset()
                data = app.leaky_trap.read()
                data = data[:,-1*(app.pre_stim_samples+app.post_stim_samples+n_excess):-1*n_excess]
                app.erp_thread.queue.put({'save_trial':data})
                app.erp_collected = True
                
            if app.changed('LastERPx100'):
                #TODO: Update the feedback to show the LastERP's value
                pass
            
    @classmethod
    def event(cls, app, phasename, event):
        if int(app.params['ERPDatabaseEnable'])==1: pass