#!/usr/bin/env python3
# Plots from paired NUCLEO-G0B1RE benchmark and Rigol recordings.
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from pathlib import Path
dest=Path(__file__).resolve().parent
blue='#294b83'
teal='#087f8c'
red='#b45342'
plt.rcParams.update({'font.size':11,'axes.spines.top':False,
                     'axes.spines.right':False,'figure.facecolor':'white',
                     'savefig.facecolor':'white'})

def save(name):
    plt.tight_layout()
    plt.savefig(dest/name,dpi=180,bbox_inches='tight')
    plt.close()

fig,ax=plt.subplots(figsize=(8.5,4.8))
freqs=['200 Hz','25 kHz']
x=np.arange(2)
a=ax.bar(x-.18,[8.79,21.79],.36,color=blue,label='TIM2 scheduler + software PA15 PWM')
b=ax.bar(x+.18,[10.18,10.18],.36,color=teal,label='TIM14 scheduler + TIM2 hardware PA15 PWM')
ax.bar_label(a,labels=['8.79%','21.79%'],padding=3,fontsize=10)
ax.bar_label(b,labels=['10.18%','10.18%'],padding=3,fontsize=10)
ax.set(xticks=x,xticklabels=freqs,ylabel='Instrumented scheduler ISR CPU time (%)',ylim=(0,26))
ax.set_title('At 25 kHz, hardware PWM removes 53% of measured ISR-core cost')
ax.legend(fontsize=9,loc='upper left')
ax.text(0.01,-.2,'NUCLEO-G0B1RE; PB8 20k steps/s; PD0 TIM16 PWM active in both runs.\nCore-only ISR timing, not total MCU CPU. Single matched runs.',transform=ax.transAxes,fontsize=8.5,color='#444')
save('tim14_isr_cpu_200hz_25khz.png')

fig,ax=plt.subplots(figsize=(8.5,4.8))
rates=np.array([40,80,120])
ax.plot(rates,[31.62,46.11,65.53],color=blue,marker='o',linewidth=2.3,markersize=8,label='TIM2 + 25 kHz software PA15 PWM')
ax.plot(rates,[20.61,40.50,59.88],color=teal,marker='s',linewidth=2.3,markersize=8,label='TIM14 + 25 kHz hardware PA15 PWM')
ax.set(xticks=rates,xlabel='Requested STEP rate (thousands of steps/s)',ylabel='Scheduler ISR-core CPU time (%)',ylim=(10,75))
ax.set_title('Paired STEP-load sweep with two 25 kHz PWM outputs')
ax.legend(loc='upper left',fontsize=9)
ax.grid(axis='y',alpha=.2)
ax.text(.01,-.2,'Four-second measured window; queued pulses extend beyond the window at some rates.\nNot proof of missed-step-free operation at high rates.',transform=ax.transAxes,fontsize=8.5,color='#444')
save('tim14_isr_cpu_step_sweep.png')

stock=[12800,12400,12400,12400,12800,12400,12400,12400,12400,13600,11600,12400,13600,11600,12000,12800,12400,14000,13200,12400,14000,10800,12800,13200,11600,11600,13600,11200,15200,11200,12800,12400,12400,13200,11600,12800,12600,13400,11600,12400,12800,12400,13200,11600,12400,14000,11200,12400,13200,12000,11200,13600,12400,14800]
hw=[12400,12400,12400,12800,12400,12400,12400,12800,12400,12400,12400,12400,12800,12400,12400,12400,12800,12400,12400,12400,12400,12800,12400,12400,12400,12800,12400,12400,12400,12400,12800,12400,12400,12400,12800,12400,12400,12400,12400,12800,12400,12400,12400,12800,12400,12400,12400,12400,12800,12400,12400,12400,12800,12400]
assert len(stock)==len(hw)==54
np.savetxt(dest/'rigol_step_intervals_80k.csv',np.column_stack((np.arange(1,55),stock,hw)),fmt='%d',delimiter=',',header='sample_index,stock_interval_ns,proposed_interval_ns',comments='')
fig,ax=plt.subplots(figsize=(10,4.8))
ax.plot(np.arange(1,55),np.array(stock)/1000,color=blue,marker='o',markersize=3,linewidth=1.5,label='TIM2 + software PA15 PWM (std 0.899 us)')
ax.plot(np.arange(1,55),np.array(hw)/1000,color=teal,marker='s',markersize=3,linewidth=1.5,label='TIM14 + hardware PA15 PWM (std 0.166 us)')
ax.axhline(12.5,color='#666',linestyle=':',label='Target: 12.50 us')
ax.set(xlabel='Measured STEP interval index (six short Rigol captures)',ylabel='STEP rising-edge interval (us)',ylim=(10.3,15.7))
ax.set_title('80k STEP/s: measured PA15 PWM coexistence on NUCLEO-G0B1RE')
ax.grid(axis='y',alpha=.2)
ax.legend(loc='upper left',fontsize=9)
ax.text(.01,-.23,'54 intervals/configuration; Rigol 200 ns sample spacing. Small-sample timing comparison,\nnot a worst-case jitter bound or proof of no missed steps.',transform=ax.transAxes,fontsize=8.5,color='#444')
save('tim14_step_80k_rigol.png')

fig,ax=plt.subplots(figsize=(8.5,4.9))
rates=np.array([80,100,110,120])
stdev_stock=[.899,1.129,1.220,1.661]
stdev_hw=[.166,.171,.189,1.828]
ax.plot(rates,stdev_stock,color=blue,marker='o',linewidth=2.3,markersize=7,label='TIM2 + software PWM')
ax.plot(rates,stdev_hw,color=teal,marker='s',linewidth=2.3,markersize=7,label='TIM14 + hardware PWM')
ax.set(xticks=rates,xlabel='Requested STEP rate (thousands of steps/s)',ylabel='Rigol observed STEP interval std. dev. (us)',ylim=(0,2.2))
ax.set_title('Benefit is workload-dependent; neither is stable at 120k')
ax.legend(loc='upper left',fontsize=9)
ax.grid(axis='y',alpha=.2)
ax.text(.01,-.19,'Short captures, uneven sample counts, 200 ns acquisition resolution;\nnot a statistical guarantee. 120k exhibits a shared short/long pattern.',transform=ax.transAxes,fontsize=8.5,color='#444')
save('tim14_step_jitter_sweep.png')
print('PLOTS',len(list(dest.glob('*.png'))),'CSV',len(stock))
