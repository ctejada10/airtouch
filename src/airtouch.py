#!/usr/bin/env python3
"""Plot data from the serial port using asyncio and pyqtgraph."""
import sys, asyncio, serial_asyncio, time, pint
import pyqtgraph as pg
import numpy as np
from   quamash       import QEventLoop, QtGui, QtCore
from   collections   import deque
from   OneEuroFilter import OneEuroFilter
from joblib					 import load
import socket


class DataGrapher(pg.GraphicsWindow):
	u = pint.UnitRegistry()

	@classmethod
	def rawPressureToHPA(cls, p, pMin, pMax):
		psi = ((p - 0x19999A) * (pMax - pMin))/(0xE66666 - 0x19999A)
		#psi  = (25 * (p - pMin))/(pMax - pMin)
		return ((psi * cls.u.psi).to(cls.u.hectopascal)).m


	def __init__(self, serialport,  *, baudrate=115200,
			outpath=None, printlines=False, firstline='', printHz=False,
			bufsize=2048, pMin = 0, pMax = 25, convert_pressure=False,
			socket_start=False):
		super().__init__()

		self.serialport       = serialport
		self.baudrate         = baudrate
		self.outpath          = outpath
		self.outfile          = None
		self.printlines       = printlines
		self.printHz          = printHz
		self.bufsize          = bufsize
		self.plotbuf          = deque(maxlen=self.bufsize)
		self.rawdata          = deque(maxlen=self.bufsize)
		self.pMin             = pMin
		self.pMax             = pMax

		self.convert_pressure = convert_pressure
		self.f          = OneEuroFilter(100, .25, .1)
		self.event      = 0
		self.run        = True
		self.filterflag = True

		#Touch event detection
		self.diffsamps    = 100 #How far back to look for diff
		self.event_thresh = 7		 #Standard Deviation value to consider an event is happening
		self.touch        = False  #Is a touch happening?
		self.touchcount   = 0			 #How many touch samples so far?
		self.touchthresh  = 1000 #Number of touch samples to check
		self.touch_data   = []		 #Data from touch to be sent to machine learning thingy
		self.touch_buf    = []
#		self.model        = load(model) # Path to compiled model file
		self.printed			= False

		self.socket = socket_start

		#Keep values to update baseline
		self.baselinebuf = deque(maxlen=self.bufsize//4)

		app  = QtGui.QApplication(sys.argv)
		loop = QEventLoop(app)
		asyncio.set_event_loop(loop)

		self.resize(800, 400)

		p = self.addPlot()
		self.plotline = p.plot(pen='y')
		self.baseline = p.plot(pen='b')
		self.show()

		if self.socket:
			# Create socket and wait for connection
			ip = ''
			port = 6969
			connected = False
			s = socket.socket()
			print('Waiting on connection')
			s.bind((ip, port))
			s.listen(1)
			self.conn, self.addr = s.accept()
			loop.run_until_complete(self.read_data())

		if self.outpath:
			with open(self.outpath, 'w') as self.outfile:
				if firstline:
					self.log('#{}'.format(firstline))
				loop.run_until_complete(self.read_data())
		else:
				if firstline:
					self.log('#{}'.format(firstline))
				loop.run_until_complete(self.read_data())


	def log(self, text):
		"""Dump text to stdout and/or a file."""
		if self.printlines:
			print(text)
		if self.outfile:
			self.outfile.write(text + '\n')


	async def read_data(self):
		reader, _ = await serial_asyncio.open_serial_connection(
				url=self.serialport, baudrate=self.baudrate)
		self.count = 0
		self.starttime = time.time()

		#Waiting for value to rise above self.risethresh if true,
		# otherwise waiting for it to fall below self.fallthresh
		self.wait_rise = True

		self.log('ts,event,sensorcount,filtpressure,rawpressure,filtering,cutoff,beta')

		while self.run:
			line = (await reader.readuntil()).strip()

			if not line:
				continue

			try:
				sensorcount = int(line)  #default looks for \n
			except ValueError:
				sys.stderr.write("Bad line: [{}]".format(line))
				continue

			t = time.time()
			if self.convert_pressure:
				data = self.rawPressureToHPA(sensorcount, self.pMin, self.pMax)
			else:
				data = sensorcount
			fdata = self.f(data, t)

			self.rawdata.append(data)
			if self.filterflag:
				self.plotbuf.append(fdata)
			else:
				self.plotbuf.append(data)

			if len(self.plotbuf) > self.diffsamps:
				ddiff = data - self.plotbuf[-self.diffsamps]
				self.touch_buf = np.array(self.plotbuf)[-self.diffsamps:]
				touch = False
				if self.wait_rise and self.touch_buf.std() > self.event_thresh:
					self.wait_rise	= False
				elif (not self.wait_rise) and self.touch_buf.std() < self.event_thresh:
					self.printed = False
					self.wait_rise = True
					self.touch		 = ddiff > 0
					print("Detected a {}".format('touch' if self.touch else 'release'))

				if self.touch:
					self.touchcount += 1
					if self.touchcount > self.touchthresh:
						# Classify
						if not self.printed:
							t = list(self.plotbuf)
							baseline = np.median(analog2pressure(np.array(t[:-self.touchthresh])))
							touch_data = np.abs(analog2pressure(np.array(t[self.touchthresh:])) - baseline) / baseline
							d = np.array([touch_data.mean()]).reshape(1, -1)
#							touch_id = self.model.predict(d)[0]
#							print(touch_id)
							print(d)
							self.printed = True

							if self.socket:
								self.conn.send('{}\n'.format(touch_id).encode())
				else:
					self.touchcount = 0

					if not self.printed:
						print('0')
						if self.socket:
							self.conn.send('0\n'.encode())
					self.printed = True

					
			if self.printHz:
				self.count += 1
				if self.count == self.bufsize//4:
					print("{:.2f} Hz".format(self.bufsize//4/(time.time() - self.starttime)))
					self.starttime = time.time()
					self.count = 0

			if self.printlines or self.outfile:
				out = ('{timestamp:.4f},{event},{sensorcount},{filtpressure:.8f},'
							 '{rawpressure:.8f},{filtering},{cutoff:.4f},{beta:.4f}').format(
								timestamp=t,
								event=self.event,
								sensorcount=sensorcount,
								filtpressure=fdata,
								rawpressure=data,
								filtering=self.filterflag,
								cutoff=self.f._OneEuroFilter__mincutoff,
								beta=self.f._OneEuroFilter__beta)
				self.log(out)

			self.plotline.setData(self.plotbuf)


	def keyPressEvent(self, ev):
		event = ev.text()
		if ev.key() == 16777216:
			self.run = False
		elif event in '[],.':
			if event == ',':
				self.f._OneEuroFilter__mincutoff -= .01
			elif event == '.':
				self.f._OneEuroFilter__mincutoff += .01
			elif event == '[':
				self.f._OneEuroFilter__beta -= .01
			elif event == ']':
				self.f._OneEuroFilter__beta += .01
			print('cutoff: {}, beta: {}'.format(self.f._OneEuroFilter__mincutoff,
				self.f._OneEuroFilter__beta))
		elif event == '/':
			self.filterflag = not self.filterflag
		elif event == '-':
			self.baseline.setData((0, self.bufsize), (self.plotbuf[-1], self.plotbuf[-1]))
		else:
			self.event = event


	def keyReleaseEvent(self, ev):
		self.event = 0

def analog2pressure(volts, vmin=.5, vmax=4.5, pmin=0, pmax=6,
		resolution=2**10, working_voltage=5):
	"""Return pressure from an analog sensor. Set p_min and p_max to the
	min and max working values of the sensor. Assumes that voltage and
	pressure have a linear relationship of v = m*p + o with m the slope
	and o the offset."""
	volts_per_bit = working_voltage/resolution
	m = (vmax - vmin)/(pmax - pmin)
	v = volts * volts_per_bit
	return (v - vmin) * 1/m



if __name__ == "__main__":
	from clize import run
	run(DataGrapher)
