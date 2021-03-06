#!/usr/bin/python
#
# Google Sheet Creator
#
# The following additional packages are required beyond the base install:
#
# python2 package requirements:
# sudo apt-get install python-configparser python-requests python-psutil python-httplib2 python-pip
# sudo pip2 install --upgrade google-api-python-client oauth2client
#
# python3 package requirements:
# sudo apt-get install python3-psutil python3-pip
# sudo pip3 install --upgrade google-api-python-client oauth2client
#
# To run -setup without local browser use this command:
#  ./googlesheet.py -setup --noauth_local_webserver
#

import os
import sys
import warnings
import re
import shutil
import time
import pickle
from distutils.dir_util import copy_tree
from tempfile import NamedTemporaryFile, mkdtemp
from subprocess import call, Popen, PIPE
from datetime import datetime
import argparse
import smtplib
import sleepgraph as sg
import tools.bugzilla as bz
import os.path as op
from tools.googleapi import setupGoogleAPIs, initGoogleAPIs, google_api_command, gdrive_find, gdrive_mkdir, gdrive_backup
from tools.parallel import MultiProcess

gslink = '=HYPERLINK("{0}","{1}")'
gsperc = '=({0}/{1})'
deviceinfo = {'suspend':dict(),'resume':dict()}
trash = []
mystarttime = time.time()

def pprint(msg, withtime=True):
	if withtime:
		print('[%05d] %s' % (time.time()-mystarttime, msg))
	else:
		print(msg)
	sys.stdout.flush()

def empty_trash():
	global trash
	for item in trash:
		if op.exists(item):
			if op.isdir(item):
				shutil.rmtree(item)
			else:
				os.remove(item)
	trash = []

def errorCheck(line):
	l = line.lower()
	for s in ['error', 'bug', 'failed']:
		if s in l:
			return True
	return False

def healthCheck(data):
	h, hmax = 0, 80
	# 40	Test Pass/Fail
	tpass = float(data['resdetail']['pass'])
	total = float(data['resdetail']['tests'])
	h = 40.0 * tpass / total
	# 10	Kernel issues
	if 'issues' in data and len(data['issues']) > 0:
		warningsonly = True
		for issue in data['issues']:
			if errorCheck(issue['line']):
				warningsonly = False
				break
		h += 5 if warningsonly else 0
	else:
		h += 10
	# 10	Suspend Time (median)
	if data['sstat'][1]:
		smax = float(data['sstat'][0])
		smed = float(data['sstat'][1])
		pval = 10.0 if smax < 1000 else 9.5
		if smed < 1000:
			h += pval
		elif smed < 2000:
			h += ((2000 - smed) / 1000) * pval
	# 20	Resume Time (median)
	if data['rstat'][1]:
		rmax = float(data['rstat'][0])
		rmed = float(data['rstat'][1])
		pval = 20.0 if rmax < 1000 else 19.5
		if rmed < 1000:
			h += pval
		elif rmed < 2000:
			h += ((2000 - rmed) / 1000) * pval
	# 20	S0ix achieved in S2idle
	if data['mode'] == 'freeze' and 'syslpi' in data and data['syslpi'] >= 0:
		hmax += 20
		h += 20.0 * float(data['syslpi'])/total
	data['health'] = int(100 * h / hmax)

def columnMap(file, head, required):
	# create map of column name to index
	colidx = dict()
	s, e, idx = head.find('<th') + 3, head.rfind('</th>'), 0
	for key in head[s:e].replace('</th>', '').split('<th'):
		name = re.sub('[^>]*>', '', key.strip().lower(), 1)
		colidx[name] = idx
		idx += 1
	for name in required:
		if name not in colidx:
			doError('"%s" column missing in %s' % (name, file))
	return colidx

def columnValues(colidx, row):
	# create an array of column values from this row
	values = []
	out = row.split('<td')
	for i in out[1:]:
		endtrim = re.sub('</td>.*', '', i.replace('\n', ''))
		value = re.sub('[^>]*>', '', endtrim, 1)
		values.append(value)
	return values

def infoDevices(folder, file, basename):
	global deviceinfo

	colidx = dict()
	html = open(file, 'r').read()
	for tblock in html.split('<div class="stamp">'):
		x = re.match('.*\((?P<t>[A-Z]*) .*', tblock)
		if not x:
			continue
		type = x.group('t').lower()
		if type not in deviceinfo:
			continue
		for dblock in tblock.split('<tr'):
			if '<th>' in dblock:
				# check for requried columns
				colidx = columnMap(file, dblock, ['device name', 'average time',
					'count', 'worst time', 'host (worst time)', 'link (worst time)'])
				continue
			if len(colidx) == 0 or '<td' not in dblock or '</td>' not in dblock:
				continue
			values = columnValues(colidx, dblock)
			x, url = re.match('<a href="(?P<u>.*)">', values[colidx['link (worst time)']]), ''
			if x:
				url = op.relpath(file.replace(basename, x.group('u')), folder)
			name = values[colidx['device name']]
			count = int(values[colidx['count']])
			avgtime = float(values[colidx['average time']].split()[0])
			wrstime = float(values[colidx['worst time']].split()[0])
			host = values[colidx['host (worst time)']]
			entry = {
				'name': name,
				'count': count,
				'total': count * avgtime,
				'worst': wrstime,
				'host': host,
				'url': url
			}
			if name in deviceinfo[type]:
				if entry['worst'] > deviceinfo[type][name]['worst']:
					deviceinfo[type][name]['worst'] = entry['worst']
					deviceinfo[type][name]['host'] = entry['host']
					deviceinfo[type][name]['url'] = entry['url']
				deviceinfo[type][name]['count'] += entry['count']
				deviceinfo[type][name]['total'] += entry['total']
			else:
				deviceinfo[type][name] = entry

def infoIssues(folder, file, basename, testcount):

	colidx = dict()
	issues, bugs = [], []
	html = open(file, 'r').read()
	tables = sg.find_in_html(html, '<table>', '</table>', False)
	if len(tables) < 1:
		return (issues, bugs)
	for issue in tables[0].split('<tr'):
		if '<th>' in issue:
			# check for requried columns
			colidx = columnMap(file, issue, ['issue', 'count', 'tests', 'first instance'])
			continue
		if len(colidx) == 0 or '<td' not in issue or '</td>' not in issue:
			continue
		values = columnValues(colidx, issue)
		x, url = re.match('<a href="(?P<u>.*)">.*', values[colidx['first instance']]), ''
		if x:
			url = op.relpath(file.replace(basename, x.group('u')), folder)
		tests = int(values[colidx['tests']])
		issues.append({
			'count': int(values[colidx['count']]),
			'tests': tests,
			'rate': float(tests)*100.0/testcount,
			'line': values[colidx['issue']],
			'url': url,
		})
	if len(tables) < 2:
		return (issues, bugs)
	for bug in tables[1].split('<tr'):
		if '<th>' in bug:
			# check for requried columns
			colidx = columnMap(file, bug, ['bugzilla', 'description', 'count', 'first instance'])
			continue
		if len(colidx) == 0 or '<td' not in bug or '</td>' not in bug:
			continue
		values = columnValues(colidx, bug)
		x, url = re.match('<a href="(?P<u>.*)">.*', values[colidx['first instance']]), ''
		if x:
			url = op.relpath(file.replace(basename, x.group('u')), folder)
		x = re.match('<a href="(?P<u>.*)">(?P<id>[0-9]*)</a>', values[colidx['bugzilla']])
		if not x:
			continue
		count = int(values[colidx['count']])
		bugs.append({
			'count': count,
			'rate': float(count)*100.0/testcount,
			'desc': values[colidx['description']],
			'bugurl': x.group('u'),
			'bugid': x.group('id'),
			'url': url,
		})
	return (issues, bugs)

def kernelRC(kernel, strict=False):
	m = re.match('(?P<v>[0-9]*\.[0-9]*\.[0-9]*).*\-rc(?P<rc>[0-9]*).*', kernel)
	if m:
		return m.group('v')+'-rc'+m.group('rc')
	m = re.match('(?P<v>[0-9]*\.[0-9]*\.[0-9]*).*', kernel)
	if m:
		return m.group('v')
	return '' if strict else kernel

def info(file, data, args):

	colidx = dict()
	desc = dict()
	resdetail = {'tests':0, 'pass': 0, 'fail': 0, 'hang': 0, 'error': 0}
	statvals = dict()
	worst = {'worst suspend device': dict(), 'worst resume device': dict()}
	starttime = endtime = 0
	extra = dict()

	# parse the html row by row
	html = open(file, 'r').read()
	for test in html.split('<tr'):
		if '<th>' in test:
			# check for requried columns
			colidx = columnMap(file, test, ['kernel', 'host', 'mode',
				'result', 'test time', 'suspend', 'resume'])
			continue
		if len(colidx) == 0 or 'class="head"' in test or '<html>' in test:
			continue
		# create an array of column values from this row
		values = []
		out = test.split('<td')
		for i in out[1:]:
			values.append(re.sub('</td>.*', '', i[1:].replace('\n', '')))
		# fill out the desc, and be sure all the tests are the same
		for key in ['kernel', 'host', 'mode']:
			val = values[colidx[key]]
			if key not in desc:
				desc[key] = val
			elif val != desc[key]:
				pprint('SKIPPING %s, multiple %ss found' % (file, key))
				return
		# count the tests and tally the various results
		resdetail[values[colidx['result']].split()[0]] += 1
		resdetail['tests'] += 1
		# find the timeline url if possible
		url = ''
		if 'detail' in colidx:
			x = re.match('<a href="(?P<u>.*)">', values[colidx['detail']])
			if x:
				link = file.replace('summary.html', x.group('u'))
				url = op.relpath(link, args.folder)
		# pull the test time from the url (host machine clock is more reliable)
		testtime = datetime.strptime(values[colidx['test time']], '%Y/%m/%d %H:%M:%S')
		if url:
			x = re.match('.*/suspend-(?P<d>[0-9]*)-(?P<t>[0-9]*)/.*', url)
			if x:
				testtime = datetime.strptime(x.group('d')+x.group('t'), '%y%m%d%H%M%S')
		if not endtime or testtime > endtime:
			endtime = testtime
		if not starttime or testtime < starttime:
			starttime = testtime
		# find the suspend/resume max/med/min values and links
		x = re.match('id="s%s(?P<s>[a-z]*)" .*>(?P<v>[0-9\.]*)' % \
			desc['mode'], values[colidx['suspend']])
		if x:
			statvals['s'+x.group('s')] = (x.group('v'), url)
		x = re.match('id="r%s(?P<s>[a-z]*)" .*>(?P<v>[0-9\.]*)' % \
			desc['mode'], values[colidx['resume']])
		if x:
			statvals['r'+x.group('s')] = (x.group('v'), url)
		# tally the worst suspend/resume device values
		for phase in worst:
			if phase not in colidx or not values[colidx[phase]]:
				continue
			idx = colidx[phase]
			if values[idx] not in worst[phase]:
				worst[phase][values[idx]] = 0
			worst[phase][values[idx]] += 1
		# tally any turbostat values if found
		for key in ['pkgpc10', 'syslpi', 'wifi']:
			if key not in colidx or not values[colidx[key]]:
				continue
			val = values[colidx[key]]
			if key not in extra:
				extra[key] = -1
			if val in ['N/A', 'TIMEOUT']:
				continue
			if extra[key] < 0:
				extra[key] = 0
			if key == 'wifi':
				if val:
					extra[key] += 1
			else:
				val = float(val.replace('%', ''))
				if val > 0:
					extra[key] += 1
	statinfo = {'s':{'val':[],'url':[]},'r':{'val':[],'url':[]}}
	for p in statinfo:
		dupval = statvals[p+'min'] if p+'min' in statvals else ('', '')
		for key in [p+'max', p+'med', p+'min']:
			val, url = statvals[key] if key in statvals else dupval
			statinfo[p]['val'].append(val)
			statinfo[p]['url'].append(url)
	cnt = 1 if resdetail['tests'] < 2 else resdetail['tests'] - 1
	avgtime = ((endtime - starttime) / cnt).total_seconds()
	data.append({
		'kernel': desc['kernel'],
		'rc': kernelRC(desc['kernel']),
		'host': desc['host'],
		'mode': desc['mode'],
		'count': resdetail['tests'],
		'date': starttime.strftime('%y%m%d'),
		'time': starttime.strftime('%H%M%S'),
		'file': file,
		'resdetail': resdetail,
		'sstat': statinfo['s']['val'],
		'rstat': statinfo['r']['val'],
		'sstaturl': statinfo['s']['url'],
		'rstaturl': statinfo['r']['url'],
		'wsd': worst['worst suspend device'],
		'wrd': worst['worst resume device'],
		'testtime': avgtime,
		'totaltime': avgtime * resdetail['tests'],
	})
	x = re.match('.*/suspend-[a-z]*-(?P<d>[0-9]*)-(?P<t>[0-9]*)-[0-9]*min/summary.html', file)
	if x:
		btime = datetime.strptime(x.group('d')+x.group('t'), '%y%m%d%H%M%S')
		data[-1]['timestamp'] = btime
	for key in extra:
		data[-1][key] = extra[key]

	dfile = file.replace('summary.html', 'summary-devices.html')
	if op.exists(dfile):
		infoDevices(args.folder, dfile, 'summary-devices.html')
	else:
		pprint('WARNING: device summary is missing:\n%s\nPlease rerun sleepgraph -summary' % dfile)

	ifile = file.replace('summary.html', 'summary-issues.html')
	if op.exists(ifile):
		data[-1]['issues'], data[-1]['bugs'] = infoIssues(args.folder, ifile,
			'summary-issues.html', data[-1]['resdetail']['tests'])
	else:
		pprint('WARNING: issues summary is missing:\n%s\nPlease rerun sleepgraph -summary' % ifile)
	healthCheck(data[-1])

def text_output(args, data, buglist, devinfo=False):
	global deviceinfo

	text = ''
	for test in sorted(data, key=lambda v:(v['kernel'],v['host'],v['mode'],v['date'],v['time'])):
		text += 'Kernel : %s\n' % test['kernel']
		text += 'Host   : %s\n' % test['host']
		text += 'Mode   : %s\n' % test['mode']
		text += 'Health : %d\n' % test['health']
		if 'timestamp' in test:
			text += '   Timestamp: %s\n' % test['timestamp']
		text += '   Duration: %.1f hours\n' % (test['totaltime'] / 3600)
		text += '   Avg test time: %.1f seconds\n' % test['testtime']
		text += '   Results:\n'
		total = test['resdetail']['tests']
		for key in ['pass', 'fail', 'hang', 'error']:
			val = test['resdetail'][key]
			if val > 0:
				p = 100*float(val)/float(total)
				text += '   - %s: %d/%d (%.2f%%)\n' % (key.upper(), val, total, p)
		for key in ['pkgpc10', 'syslpi', 'wifi']:
			if key not in test:
				continue
			if test[key] < 0:
				text += '   %s: UNSUPPORTED\n' % (key.upper())
			else:
				text += '   %s: %d/%d\n' % \
					(key.upper(), test[key], test['resdetail']['tests'])
		if test['sstat'][2]:
			text += '   Suspend: Max=%s, Med=%s, Min=%s\n' % \
				(test['sstat'][0], test['sstat'][1], test['sstat'][2])
		else:
			text += '   Suspend: N/A\n'
		if test['rstat'][2]:
			text += '   Resume: Max=%s, Med=%s, Min=%s\n' % \
				(test['rstat'][0], test['rstat'][1], test['rstat'][2])
		else:
			text += '   Resume: N/A\n'
		text += '   Worst Suspend Devices:\n'
		wsus = test['wsd']
		for i in sorted(wsus, key=lambda k:wsus[k], reverse=True):
			text += '   - %s (%d times)\n' % (i, wsus[i])
		text += '   Worst Resume Devices:\n'
		wres = test['wrd']
		for i in sorted(wres, key=lambda k:wres[k], reverse=True):
			text += '   - %s (%d times)\n' % (i, wres[i])
		if 'issues' not in test or len(test['issues']) < 1:
			continue
		text += '   Issues found in dmesg logs:\n'
		issues = test['issues']
		for e in sorted(issues, key=lambda v:v['count'], reverse=True):
			text += '   (x%d t%d) %s\n' % (e['count'], e['tests'], e['line'])

	if args.bugzilla:
		text += '\n'
		for id in sorted(buglist, key=lambda k:(buglist[k]['worst'], int(k)), reverse=True):
			b = buglist[id]
			text += '%6s: %s\n' % (id, b['desc'])
			if 'match' not in buglist[id]:
				continue
			matches = buglist[id]['match']
			for m in sorted(matches, key=lambda k:(k['rate'], k['count'], k['host'], k['mode']), reverse=True):
				text += '    %s %s %s - [%d / %d]\n' % (m['kernel'], m['host'],
					m['testname'], m['count'], m['total'])

	if devinfo:
		for type in sorted(deviceinfo, reverse=True):
			text += '\n%-50s %10s %9s %5s %s\n' % (type.upper(), 'WORST', 'AVG', 'COUNT', 'HOST')
			devlist = deviceinfo[type]
			for name in sorted(devlist, key=lambda k:devlist[k]['worst'], reverse=True):
				d = deviceinfo[type][name]
				text += '%50s %10.3f %9.3f %5d %s\n' % \
					(d['name'], d['worst'], d['average'], d['count'], d['host'])

	return text

def get_url(htmlfile, urlprefix):
	if not urlprefix:
		link = htmlfile
	else:
		link = op.join(urlprefix, htmlfile)
	return '<a href="%s">html</a>' % link

def cellColor(errcond, warncond):
	if errcond:
		return 'f77'
	if warncond:
		return 'ff7'
	return '7f7'

def html_output(args, data, buglist):
	urlprefix = args.urlprefix
	issues = worst = False
	html = '<!DOCTYPE html>\n<html>\n<head>\n\
		<meta http-equiv="content-type" content="text/html; charset=UTF-8">\n\
		<title>SleepGraph Summary of Summaries</title>\n\
		<style type=\'text/css\'>\n\
			table {width:100%; border-collapse: collapse;}\n\
			th {border: 2px solid black;background:#622;color:white;}\n\
			td {font: 14px "Times New Roman";}\n\
			c {font: 12px "Times New Roman";}\n\
			ul {list-style-type: none;}\n\
		</style>\n</head>\n<body>\n'

	# generate the header text
	slink, uniq = '', dict()
	for test in data:
		for key in ['kernel', 'host', 'mode']:
			if key not in uniq:
				uniq[key] = test[key]
			elif test[key] != uniq[key]:
				uniq[key] = ''
		if not args.htmlonly and not slink:
			slink = gdrive_link(args.spath, test, '{kernel}')
	links = []
	for key in ['kernel', 'host', 'mode']:
		if key in uniq and uniq[key]:
			link = '%s%s=%s' % (key[0].upper(), key[1:], uniq[key])
			if slink:
				link = '<a href="%s">%s</a>' % (slink, link)
			links.append(link)
	if len(links) < 1:
		link = '%d multitest runs' % len(data)
		if slink:
			link = '<a href="%s">%s</a>' % (slink, link)
		links.append(link)
	html += 'Sleepgraph Stress Test Summary: %s<br><br>\n' % (','.join(links))

	# generate the main text
	colspan = 12 if worst else 10
	th = '\t<th>{0}</th>\n'
	td = '\t<td nowrap align=center>{0}</td>\n'
	tdm = '\t<td nowrap align=center style="border: 1px solid black;">{0}</td>\n'
	tdmc = '\t<td nowrap align=center style="border: 1px solid black;background:#{1};">{0}</td>\n'
	tdml = '\t<td nowrap style="border: 1px solid black;">{0}</td>\n'
	html += '<table style="border:1px solid black;">\n'
	html += '<tr>\n' + th.format('Kernel') + th.format('Host') +\
		th.format('Mode') + th.format('Test Data') + th.format('Duration') +\
		th.format('Health') + th.format('Results') + th.format('Issues') +\
		th.format('Suspend Time') + th.format('Resume Time')
	if worst:
		html += th.format('Worst Suspend Devices') + th.format('Worst Resume Devices')
	html += '</tr>\n'
	num = 0
	for test in sorted(data, key=lambda v:(v['health'],v['host'],v['mode'],v['kernel'],v['date'],v['time'])):
		links = dict()
		for key in ['kernel', 'host', 'mode']:
			glink = ''
			if not args.htmlonly:
				glink = gdrive_link(args.tpath, test, '{%s}'%key)
			if glink:
				links[key] = '<a href="%s">%s</a>' % (glink, test[key])
			else:
				links[key] = test[key]
		glink = ''
		if not args.htmlonly:
			glink = gdrive_link(args.tpath, test)
		gpath = gdrive_path('{date}{time}', test)
		if glink:
			links['test'] = '<a href="%s">%s</a>' % (glink, gpath)
		else:
			links['test']= gpath
		trs = '<tr style="background-color:#ddd;">\n' if num % 2 == 1 else '<tr>\n'
		num += 1
		html += trs
		html += tdm.format(links['kernel'])
		html += tdm.format(links['host'])
		html += tdm.format(links['mode'])
		html += tdm.format(links['test'])
		dur = '<table><tr>%s</tr><tr>%s</tr></table>' % \
			(td.format('%.1f hours' % (test['totaltime'] / 3600)),
			td.format('%d x %.1f sec' % (test['resdetail']['tests'], test['testtime'])))
		html += tdm.format(dur)
		html += tdmc.format(test['health'],
			cellColor(test['health'] < 40, test['health'] < 90))
		reshtml = '<table>'
		total = test['resdetail']['tests']
		passfound = failfound = False
		for key in ['pass', 'fail', 'hang', 'error']:
			val = test['resdetail'][key]
			if val < 1:
				continue
			if key == 'pass':
				passfound = True
			else:
				failfound = True
			p = 100*float(val)/float(total)
			reshtml += '<tr>%s</tr>' % \
				td.format('%s: %d/%d <c>(%.2f%%)</c>' % (key.upper(), val, total, p))
		html += tdmc.format(reshtml+'</table>',
			cellColor(not passfound, passfound and failfound))
		if 'issues' in test and len(test['issues']) > 0:
			ihtml = '<table>'
			warnings = errors = 0
			for issue in test['issues']:
				if errorCheck(issue['line']):
					errors += 1
				else:
					warnings += 1
			if errors > 0:
				ihtml += '<tr>' + td.format('%d ERROR%s' %\
					(errors, 'S' if errors > 1 else '')) + '</tr>'
			if warnings > 0:
				ihtml += '<tr>' + td.format('%d WARNING%s' %\
					(warnings, 'S' if warnings > 1 else '')) + '</tr>'
			html += tdmc.format(ihtml+'</table>', cellColor(errors > 0, warnings > 0))
		else:
			html += tdmc.format('NONE', '7f7')
		for s in ['sstat', 'rstat']:
			if test[s][2]:
				html += tdmc.format('<table><tr>%s</tr><tr>%s</tr><tr>%s</tr></table>' %\
					(td.format('Max=%s' % test[s][0]),
					td.format('Med=%s' % test[s][1]),
					td.format('Min=%s' % test[s][2])),
					cellColor(float(test[s][1]) >= 2000, float(test[s][1]) >= 1000))
			else:
				html += tdmc.format('N/A', 'f77')
		if worst:
			for entry in ['wsd', 'wrd']:
				tdhtml = '<ul style="list-style-type: circle; font-size: 10px; padding: 0 0 0 20px;">'
				list = test[entry]
				for i in sorted(list, key=lambda k:list[k], reverse=True):
					tdhtml += '<li>%s (x%d)</li>' % (i, list[i])
				html += tdml.format(tdhtml+'</ul>')
		html += '</tr>\n'
		if not issues or 'issues' not in test:
			continue
		html += '%s<td colspan=%d><table border=1 width="100%%">' % (trs, colspan)
		html += '%s<td colspan=%d style="width:90%%;"><b>Issues found</b></td><td><b>Count</b></td><td><b>html</b></td>\n</tr>' % (trs, colspan)
		issues = test['issues']
		if len(issues) > 0:
			for e in sorted(issues, key=lambda v:v['tests'], reverse=True):
				html += '%s<td colspan=%d style="font: 12px Courier;">%s</td><td>%d times</td><td>%s</td></tr>\n' % \
					(trs, colspan, e['line'], e['tests'], get_url(e['url'], urlprefix))
		else:
			html += '%s<td colspan=%d>NONE</td></tr>\n' % (trs, colspan+2)
		html += '</table></td></tr>\n'
	html += '</table><br>\n'

	if args.bugzilla:
		html += 'Open Bugzilla Issues Summary: %d total<br><br>\n' % (len(buglist))
		html += '<table style="border:1px solid black;">\n'
		html += '<tr>\n' + th.format('Bugzilla') + th.format('Description') +\
			th.format('Kernel') + th.format('Host') + th.format('Test Run') +\
			th.format('Count') + th.format('Failure Rate') + th.format('First Instance') + '</tr>\n'
		for id in sorted(buglist, key=lambda k:(buglist[k]['worst'], int(k)), reverse=True):
			b = buglist[id]
			bugurl = '<a href="%s">%s</a>' % (b['url'], id)
			trh = '<tr style="background-color:#ccc;border:1px solid black;">'
			html += '%s\n%s<td colspan=7 style="border: 1px solid black;">%s</td>\n</tr>' % \
				(trh, tdm.format(bugurl), b['desc'])
			if 'match' not in buglist[id]:
				continue
			num = 0
			matches = buglist[id]['match']
			for m in sorted(matches, key=lambda k:(k['rate'], k['count'], k['host'], k['mode']), reverse=True):
				trs = '<tr style="background-color:#ddd;">\n' if num % 2 == 1 else '<tr>\n'
				num += 1
				if m['testlink']:
					testlink = '<a href="%s">%s</a>' % (m['testlink'], m['testname'])
				else:
					testlink = m['testname']
				if m['html'].startswith('http'):
					timeline = '<a href="%s">%s</a>' % (m['html'], 'html')
				else:
					timeline = m['html']
				count = '%d' % m['count']
				rate = '%d/%d (%.2f%%)' % (m['count'], m['total'],
					100*float(m['count'])/float(m['total']))
				count = tdmc.format(count, cellColor(m['count'] > 0, False))
				rate = tdmc.format(rate, cellColor(m['count'] > 0, False))
				timeline = tdmc.format(timeline, cellColor(m['count'] > 0, False))
				html += trs + td.format('') + td.format('') +\
					tdm.format(m['kernel']) + tdm.format(m['host']) +\
					tdm.format(testlink) + count + rate + timeline + '\n</tr>'
		html += '</table>\n'

	return html + '</body>\n</html>\n'

def send_mail(server, sender, receiver, type, subject, contents):
	message = \
		'From: %s\n'\
		'To: %s\n'\
		'MIME-Version: 1.0\n'\
		'Content-type: %s\n'\
		'Subject: %s\n\n' % (sender, receiver, type, subject)
	if ',' in receiver:
		receivers = receiver.split(',')
	else:
		receivers = receiver.split(';')
	message += contents
	smtpObj = smtplib.SMTP(server, 25)
	smtpObj.sendmail(sender, receivers, message)

def gdrive_path(outpath, data, focus=''):
	desc = dict()
	for key in ['rc','kernel','host','mode','count','date','time']:
		if key in data:
			desc[key] = data[key]
	if focus and outpath.find(focus) < 0:
		gpath = ''
	elif focus:
		idx = outpath.find('/', outpath.find(focus))
		if idx >= 0:
			gpath = outpath[:idx].format(**desc)
		else:
			gpath = outpath.format(**desc)
	else:
		gpath = outpath.format(**desc)
	return gpath

def gdrive_link(outpath, data, focus=''):
	gpath = gdrive_path(outpath, data, focus)
	if not gpath:
		return ''
	linkfmt = 'https://drive.google.com/open?id={0}'
	id = gdrive_find(gpath)
	if id:
		return linkfmt.format(id)
	return ''

def gzipFile(file):
	shutil.copy(file, '/tmp')
	out = op.join('/tmp', op.basename(file))
	if not op.exists(out):
		return file
	res = call('gzip -f --best '+out, shell=True)
	if res != 0:
		return file
	out += '.gz'
	if not op.exists(out):
		return file
	return out

def formatSpreadsheet(id, urlprefix=True):
	hidx = 5 if urlprefix else 4
	highlight_range = {
		'sheetId': 1,
		'startRowIndex': 1,
		'startColumnIndex': 5,
		'endColumnIndex': 6,
	}
	bugstatus_range = {
		'sheetId': 3,
		'startRowIndex': 1,
		'startColumnIndex': 2,
		'endColumnIndex': 3,
	}
	sigdig_range = {
		'sheetId': 1,
		'startRowIndex': 1,
		'startColumnIndex': 7,
		'endColumnIndex': 13,
	}
	requests = [{
		'addConditionalFormatRule': {
			'rule': {
				'ranges': [ highlight_range ],
				'booleanRule': {
					'condition': {
						'type': 'TEXT_NOT_CONTAINS',
						'values': [ { 'userEnteredValue': 'pass' } ]
					},
					'format': {
						'textFormat': { 'foregroundColor': { 'red': 1.0 } }
					}
				}
			},
			'index': 0
		},
		'addConditionalFormatRule': {
			'rule': {
				'ranges': [ bugstatus_range ],
				'booleanRule': {
					'condition': {
						'type': 'TEXT_NOT_CONTAINS',
						'values': [ { 'userEnteredValue': 'PASS' } ]
					},
					'format': {
						'textFormat': { 'foregroundColor': { 'red': 1.0 } }
					}
				}
			},
			'index': 1
		}
	},
	{'updateBorders': {
		'range': {'sheetId': 0, 'startRowIndex': 0, 'endRowIndex': hidx,
			'startColumnIndex': 0, 'endColumnIndex': 3},
		'top': {'style': 'SOLID', 'width': 3},
		'left': {'style': 'SOLID', 'width': 3},
		'bottom': {'style': 'SOLID', 'width': 2},
		'right': {'style': 'SOLID', 'width': 2}},
	},
	{'updateBorders': {
		'range': {'sheetId': 0, 'startRowIndex': hidx, 'endRowIndex': hidx+1,
			'startColumnIndex': 0, 'endColumnIndex': 3},
		'bottom': {'style': 'DASHED', 'width': 1}},
	},
	{
		'repeatCell': {
			'range': sigdig_range,
			'cell': {
				'userEnteredFormat': {
					'numberFormat': {
						'type': 'NUMBER',
						'pattern': '0.000'
					}
				}
			},
			'fields': 'userEnteredFormat.numberFormat'
		},
	},
	{
		'repeatCell': {
			'range': {
				'sheetId': 3, 'startRowIndex': 1,
				'startColumnIndex': 4, 'endColumnIndex': 5,
			},
			'cell': {
				'userEnteredFormat': {
					'numberFormat': {'type': 'NUMBER', 'pattern': '0.00%;0%;0%'},
					'horizontalAlignment': 'RIGHT'
				}
			},
			'fields': 'userEnteredFormat.numberFormat,userEnteredFormat.horizontalAlignment'
		}
	},
	{
		'repeatCell': {
			'range': {
				'sheetId': 2, 'startRowIndex': 1,
				'startColumnIndex': 4, 'endColumnIndex': 5,
			},
			'cell': {
				'userEnteredFormat': {
					'numberFormat': {'type': 'NUMBER', 'pattern': '0.00%;0%;0%'},
					'horizontalAlignment': 'RIGHT'
				}
			},
			'fields': 'userEnteredFormat.numberFormat,userEnteredFormat.horizontalAlignment'
		}
	},
	{'autoResizeDimensions': {'dimensions': {'sheetId': 0,
		'dimension': 'COLUMNS', 'startIndex': 0, 'endIndex': 3}}},
	{'autoResizeDimensions': {'dimensions': {'sheetId': 1,
		'dimension': 'COLUMNS', 'startIndex': 0, 'endIndex': 16}}},
	{'autoResizeDimensions': {'dimensions': {'sheetId': 2,
		'dimension': 'COLUMNS', 'startIndex': 0, 'endIndex': 12}}},
	{'autoResizeDimensions': {'dimensions': {'sheetId': 3,
		'dimension': 'COLUMNS', 'startIndex': 0, 'endIndex': 6}}},
	{'autoResizeDimensions': {'dimensions': {'sheetId': 4,
		'dimension': 'COLUMNS', 'startIndex': 0, 'endIndex': 6}}},
	{'autoResizeDimensions': {'dimensions': {'sheetId': 5,
		'dimension': 'COLUMNS', 'startIndex': 0, 'endIndex': 6}}
	}]
	body = {
		'requests': requests
	}
	response = google_api_command('formatsheet', id, body)
	pprint('{0} cells updated.'.format(len(response.get('replies'))));

def createSpreadsheet(testruns, devall, issues, mybugs, folder, urlhost, title, useturbo, usewifi):
	pid = gdrive_find(folder)
	gdrive_backup(folder, title)

	# create the headers row
	headers = [
		['#','Mode','Host','Kernel','Test Start','Result','Kernel Issues','Suspend',
		'Resume','Worst Suspend Device','ms','Worst Resume Device','ms'],
		['Kernel Issue', 'Hosts', 'Count', 'Tests', 'Fail Rate', 'First Instance'],
		['Device Name', 'Average Time', 'Count', 'Worst Time', 'Host (worst time)', 'Link (worst time)'],
		['Bugzilla', 'Description', 'Status', 'Count', 'Rate', 'First Instance']
	]
	if useturbo:
		headers[0].append('PkgPC10')
		headers[0].append('SysLPI')
	if usewifi:
		headers[0].append('Wifi')
	headers[0].append('Timeline')

	headrows = []
	for header in headers:
		headrow = []
		for name in header:
			headrow.append({
				'userEnteredValue':{'stringValue':name},
				'userEnteredFormat':{
					'textFormat': {'bold': True},
					'horizontalAlignment':'CENTER',
					'borders':{'bottom':{'style':'SOLID'}},
				},
			})
		headrows.append(headrow)

	# assemble the issues in the spreadsheet
	issuedata = [{'values':headrows[1]}]
	for e in sorted(issues, key=lambda v:v['tests'], reverse=True):
		r = {'values':[
			{'userEnteredValue':{'stringValue':e['line']}},
			{'userEnteredValue':{'numberValue':len(e['urls'])}},
			{'userEnteredValue':{'numberValue':e['count']}},
			{'userEnteredValue':{'numberValue':e['tests']}},
			{'userEnteredValue':{'formulaValue':gsperc.format(e['tests'], len(testruns))}},
		]}
		for host in e['urls']:
			url = op.join(urlhost, e['urls'][host][0]) if urlhost else e['urls'][host][0]
			r['values'].append({
				'userEnteredValue':{'formulaValue':gslink.format(url, host)}
			})
		issuedata.append(r)

	# assemble the bugs in the spreadsheet
	bugdata = [{'values':headrows[3]}]
	for b in sorted(mybugs, key=lambda v:(v['count'], int(v['id'])), reverse=True):
		if b['found']:
			status = 'FAIL'
			url = op.join(urlhost, b['found']) if urlhost else b['found']
			timeline = {'formulaValue':gslink.format(url, 'html')}
		else:
			status = 'PASS'
			timeline = {'stringValue':''}
		r = {'values':[
			{'userEnteredValue':{'formulaValue':gslink.format(b['bugurl'], b['id'])}},
			{'userEnteredValue':{'stringValue':b['desc']}},
			{'userEnteredValue':{'stringValue':status}},
			{'userEnteredValue':{'numberValue':b['count']}},
			{'userEnteredValue':{'formulaValue':gsperc.format(b['count'], len(testruns))}},
			{'userEnteredValue':timeline},
		]}
		bugdata.append(r)

	# assemble the device data into spreadsheets
	limit = 1
	devdata = {
		'suspend': [{'values':headrows[2]}],
		'resume': [{'values':headrows[2]}],
	}
	for type in sorted(devall, reverse=True):
		devlist = devall[type]
		for name in sorted(devlist, key=lambda k:devlist[k]['worst'], reverse=True):
			data = devall[type][name]
			avg = data['average']
			if avg < limit:
				continue
			url = op.join(urlhost, data['url']) if urlhost else data['url']
			r = {'values':[
				{'userEnteredValue':{'stringValue':data['name']}},
				{'userEnteredValue':{'numberValue':float('%.3f' % avg)}},
				{'userEnteredValue':{'numberValue':data['count']}},
				{'userEnteredValue':{'numberValue':data['worst']}},
				{'userEnteredValue':{'stringValue':data['host']}},
				{'userEnteredValue':{'formulaValue':gslink.format(url, 'html')}},
			]}
			devdata[type].append(r)

	# assemble the entire spreadsheet into testdata
	i = 1
	results = []
	desc = {'summary': op.join(urlhost, 'summary.html')}
	testdata = [{'values':headrows[0]}]
	for test in sorted(testruns, key=lambda v:(v['mode'], v['host'], v['kernel'], v['time'])):
		for key in ['host', 'mode', 'kernel']:
			if key not in desc:
				desc[key] = test[key]
		if test['result'] not in desc:
			if test['result'].startswith('fail ') and 'fail' not in results:
				results.append('fail')
			results.append(test['result'])
			desc[test['result']] = 0
		desc[test['result']] += 1
		url = op.join(urlhost, test['url'])
		r = {'values':[
			{'userEnteredValue':{'numberValue':i}},
			{'userEnteredValue':{'stringValue':test['mode']}},
			{'userEnteredValue':{'stringValue':test['host']}},
			{'userEnteredValue':{'stringValue':test['kernel']}},
			{'userEnteredValue':{'stringValue':test['time']}},
			{'userEnteredValue':{'stringValue':test['result']}},
			{'userEnteredValue':{'stringValue':test['issues']}},
			{'userEnteredValue':{'numberValue':float(test['suspend'])}},
			{'userEnteredValue':{'numberValue':float(test['resume'])}},
			{'userEnteredValue':{'stringValue':test['sus_worst']}},
			{'userEnteredValue':{'numberValue':float(test['sus_worsttime'])}},
			{'userEnteredValue':{'stringValue':test['res_worst']}},
			{'userEnteredValue':{'numberValue':float(test['res_worsttime'])}},
		]}
		if useturbo:
			for key in ['pkgpc10', 'syslpi']:
				val = test[key] if key in test else ''
				r['values'].append({'userEnteredValue':{'stringValue':val}})
				if key not in desc:
					results.append(key)
					desc[key] = -1
				if val and val != 'N/A':
					if desc[key] < 0:
						desc[key] = 0
					val = float(val.replace('%', ''))
					if val > 0:
						desc[key] += 1
		if usewifi:
			val = test['wifi'] if 'wifi' in test else ''
			r['values'].append({'userEnteredValue':{'stringValue':val}})
			if 'wifi' not in desc:
				results.append('wifi')
				desc['wifi'] = -1
			if val:
				if desc['wifi'] < 0:
					desc['wifi'] = 0
				if val.lower() != 'timeout':
					desc['wifi'] += 1
		r['values'].append({'userEnteredValue':{'formulaValue':gslink.format(url, 'html')}})
		testdata.append(r)
		i += 1
	total = i - 1
	desc['total'] = '%d' % total
	desc['issues'] = '%d' % len(issues)
	fail = 0
	for key in results:
		if key not in desc:
			continue
		val = desc[key]
		perc = 100.0*float(val)/float(total)
		if perc >= 0:
			desc[key] = '%d (%.1f%%)' % (val, perc)
		else:
			desc[key] = 'disabled'
		if key.startswith('fail '):
			fail += val
	if fail:
		perc = 100.0*float(fail)/float(total)
		desc['fail'] = '%d (%.1f%%)' % (fail, perc)

	# create the summary page info
	summdata = []
	comments = {
		'host':'hostname of the machine where the tests were run',
		'mode':'low power mode requested with write to /sys/power/state',
		'kernel':'kernel version or release candidate used (+ means code is newer than the rc)',
		'total':'total number of tests run',
		'summary':'html summary from sleepgraph',
		'pass':'percent of tests where %s was entered successfully' % testruns[0]['mode'],
		'fail':'percent of tests where %s was NOT entered' % testruns[0]['mode'],
		'hang':'percent of tests where the system is unrecoverable (network lost, no data generated on target)',
		'error':'percent of tests where sleepgraph failed to finish (from instability after resume or tool failure)',
		'issues':'number of unique kernel issues found in test dmesg logs',
		'pkgpc10':'percent of tests where PC10 was entered (disabled means PC10 is not supported, hence 0 percent)',
		'syslpi':'percent of tests where S0IX mode was entered (disabled means S0IX is not supported, hence 0 percent)',
		'wifi':'percent of tests where wifi successfully reconnected after resume',
	}
	# sort the results keys
	pres = ['pass'] if 'pass' in results else []
	fres = []
	for key in results:
		if key.startswith('fail'):
			fres.append(key)
	pres += sorted(fres)
	pres += ['hang'] if 'hang' in results else []
	pres += ['error'] if 'error' in results else []
	pres += ['pkgpc10'] if 'pkgpc10' in results else []
	pres += ['syslpi'] if 'syslpi' in results else []
	pres += ['wifi'] if 'wifi' in results else []
	# add to the spreadsheet
	for key in ['host', 'mode', 'kernel', 'summary', 'issues', 'total'] + pres:
		comment = comments[key] if key in comments else ''
		if key.startswith('fail '):
			comment = 'percent of tests where %s NOT entered (aborted in %s)' % (testruns[0]['mode'], key.split()[-1])
		if key == 'summary':
			if not urlhost:
				continue
			val, fmt = gslink.format(desc[key], key), 'formulaValue'
		else:
			val, fmt = desc[key], 'stringValue'
		r = {'values':[
			{'userEnteredValue':{'stringValue':key},
				'userEnteredFormat':{'textFormat': {'bold': True}}},
			{'userEnteredValue':{fmt:val}},
			{'userEnteredValue':{'stringValue':comment},
				'userEnteredFormat':{'textFormat': {'italic': True}}},
		]}
		summdata.append(r)

	# create the spreadsheet
	data = {
		'properties': {
			'title': title
		},
		'sheets': [
			{
				'properties': {'sheetId': 0, 'title': 'Summary'},
				'data': [
					{'startRow': 0, 'startColumn': 0, 'rowData': summdata}
				]
			},
			{
				'properties': {'sheetId': 1, 'title': 'Test Data'},
				'data': [
					{'startRow': 0, 'startColumn': 0, 'rowData': testdata}
				]
			},
			{
				'properties': {'sheetId': 2, 'title': 'Kernel Issues'},
				'data': [
					{'startRow': 0, 'startColumn': 0, 'rowData': issuedata}
				]
			},
			{
				'properties': {'sheetId': 3, 'title': 'Bugzilla'},
				'data': [
					{'startRow': 0, 'startColumn': 0, 'rowData': bugdata}
				]
			},
			{
				'properties': {'sheetId': 4, 'title': 'Suspend Devices'},
				'data': [
					{'startRow': 0, 'startColumn': 0, 'rowData': devdata['suspend']}
				]
			},
			{
				'properties': {'sheetId': 5, 'title': 'Resume Devices'},
				'data': [
					{'startRow': 0, 'startColumn': 0, 'rowData': devdata['resume']}
				]
			},
		],
		'namedRanges': [
			{'name':'Test', 'range':{'sheetId':1,'startColumnIndex':0,'endColumnIndex':1}},
		],
	}
	sheet = google_api_command('createsheet', data)
	if 'spreadsheetId' not in sheet:
		return ''
	id = sheet['spreadsheetId']

	# special formatting
	formatSpreadsheet(id, urlhost)

	# move the spreadsheet into its proper folder
	file = google_api_command('move', id, pid)
	pprint('spreadsheet id: %s' % id)
	if 'spreadsheetUrl' not in sheet:
		return id
	return sheet['spreadsheetUrl']

def summarizeBuglist(args, data, buglist):
	urlprefix = args.urlprefix
	for test in sorted(data, key=lambda v:(v['kernel'],v['host'],v['mode'],v['date'],v['time'])):
		if 'bugs' not in test:
			continue
		testname = gdrive_path('{mode}-x{count}', test)
		testlink = testname if args.htmlonly else gdrive_link(args.tpath, test)
		bugs, total = test['bugs'], test['resdetail']['tests']
		for b in sorted(bugs, key=lambda v:v['rate'], reverse=True):
			id, count, rate = b['bugid'], b['count'], b['rate']
			url = op.join(urlprefix, b['url']) if urlprefix and b['url'] else b['url']
			if id not in buglist:
				buglist[id] = {'desc': b['desc'], 'url': b['bugurl']}
			if 'match' not in buglist[id]:
				buglist[id]['match'] = []
			buglist[id]['match'].append({
				'kernel': test['kernel'],
				'host': test['host'],
				'mode': test['mode'],
				'count': count,
				'rate': rate,
				'total': total,
				'html': url,
				'testlink': testlink,
				'testname': testname,
			})
			if rate > buglist[id]['worst']:
				buglist[id]['worst'] = rate
			buglist[id]['matches'] = len(buglist[id]['match'])

def gsissuesort(k):
	tests = k['values'][5]['userEnteredValue']['numberValue']
	val = k['values'][6]['userEnteredValue']['formulaValue'][2:-1].split('/')
	perc = float(val[0])*100.0/float(val[1])
	return (perc, tests)

def createSummarySpreadsheet(args, data, deviceinfo, buglist):
	gpath = gdrive_path(args.spath, data[0])
	dir, title = op.dirname(gpath), op.basename(gpath)
	kfid = gdrive_mkdir(dir)
	if not kfid:
		pprint('MISSING on google drive: %s' % dir)
		return False

	gdrive_backup(dir, title)

	pprint('sorting the data into tabs')
	hosts = []
	for test in data:
		if test['host'] not in hosts:
			hosts.append(test['host'])

	# create the headers row
	headers = [
		['Kernel','Host','Mode','Test Detail','Health','Duration','Avg(t)',
			'Total','Issues','Pass','Fail', 'Hang','Error','PkgPC10','Syslpi',
			'Wifi','Smax','Smed','Smin','Rmax','Rmed','Rmin'],
		['Host','Mode','Test Detail','Kernel Issue','Count','Tests','Fail Rate','First instance'],
		['Device','Average Time','Count','Worst Time','Host (worst time)','Link (worst time)'],
		['Device','Count']+hosts,
		['Bugzilla','Description','Kernel','Host','Test Run','Count','Rate','First instance'],
	]
	headrows = []
	for header in headers:
		headrow = []
		for name in header:
			headrow.append({
				'userEnteredValue':{'stringValue':name},
				'userEnteredFormat':{
					'textFormat': {'bold': True},
					'horizontalAlignment':'CENTER',
					'borders':{'bottom':{'style':'SOLID'}},
				},
			})
		headrows.append(headrow)

	urlprefix = args.urlprefix
	gslinkval = '=HYPERLINK("{0}",{1})'
	s0data = [{'values':headrows[0]}]
	s1data = []
	hostlink = dict()
	worst = {'wsd':dict(), 'wrd':dict()}
	for test in sorted(data, key=lambda v:(v['kernel'],v['host'],v['mode'],v['date'],v['time'])):
		extra = {
			'pkgpc10':{'stringValue': ''},
			'syslpi':{'stringValue': ''},
			'wifi':{'stringValue': ''}
		}
		# Worst Suspend/Resume Devices tabs data
		for entry in worst:
			for dev in test[entry]:
				if dev not in worst[entry]:
					worst[entry][dev] = {'count': 0}
					for h in hosts:
						worst[entry][dev][h] = 0
				worst[entry][dev]['count'] += test[entry][dev]
				worst[entry][dev][test['host']] += test[entry][dev]
		statvals = []
		for entry in ['sstat', 'rstat']:
			for i in range(3):
				if test[entry][i]:
					val = float(test[entry][i])
					if urlprefix:
						url = op.join(urlprefix, test[entry+'url'][i])
						statvals.append({'formulaValue':gslinkval.format(url, val)})
					else:
						statvals.append({'numberValue':val})
				else:
					statvals.append({'stringValue':''})
		# test data tab
		linkcell = dict()
		for key in ['kernel', 'host', 'mode']:
			glink = gdrive_link(args.tpath, test, '{%s}'%key)
			if glink:
				linkcell[key] = {'formulaValue':gslink.format(glink, test[key])}
			else:
				linkcell[key] = {'stringValue':test[key]}
		glink = gdrive_link(args.tpath, test)
		gpath = gdrive_path('{date}{time}', test)
		if glink:
			linkcell['test'] = {'formulaValue':gslink.format(glink, gpath)}
		else:
			linkcell['test'] = {'stringValue':gpath}
		rd = test['resdetail']
		for key in ['pkgpc10', 'syslpi', 'wifi']:
			if key in test:
				if test[key] >= 0:
					extra[key] = {'formulaValue':gsperc.format(test[key], rd['tests'])}
				else:
					extra[key] = {'stringValue': 'disabled'}
		icount = len(test['issues']) if 'issues' in test else 0
		r = {'values':[
			{'userEnteredValue':linkcell['kernel']},
			{'userEnteredValue':linkcell['host']},
			{'userEnteredValue':linkcell['mode']},
			{'userEnteredValue':linkcell['test']},
			{'userEnteredValue':{'numberValue':test['health']}},
			{'userEnteredValue':{'stringValue':'%.1f hours' % (test['totaltime']/3600)}},
			{'userEnteredValue':{'stringValue':'%.1f sec' % test['testtime']}},
			{'userEnteredValue':{'numberValue':rd['tests']}},
			{'userEnteredValue':{'numberValue':icount}},
			{'userEnteredValue':{'formulaValue':gsperc.format(rd['pass'], rd['tests'])}},
			{'userEnteredValue':{'formulaValue':gsperc.format(rd['fail'], rd['tests'])}},
			{'userEnteredValue':{'formulaValue':gsperc.format(rd['hang'], rd['tests'])}},
			{'userEnteredValue':{'formulaValue':gsperc.format(rd['error'], rd['tests'])}},
			{'userEnteredValue':extra['pkgpc10']},
			{'userEnteredValue':extra['syslpi']},
			{'userEnteredValue':extra['wifi']},
			{'userEnteredValue':statvals[0]},
			{'userEnteredValue':statvals[1]},
			{'userEnteredValue':statvals[2]},
			{'userEnteredValue':statvals[3]},
			{'userEnteredValue':statvals[4]},
			{'userEnteredValue':statvals[5]},
		]}
		s0data.append(r)
		# kernel issues tab
		if 'issues' not in test:
			continue
		issues = test['issues']
		for e in sorted(issues, key=lambda k:(k['rate'], k['tests']), reverse=True):
			if urlprefix:
				url = op.join(urlprefix, e['url'])
				html = {'formulaValue':gslink.format(url, 'html')}
			else:
				html = {'stringValue':e['url']}
			r = {'values':[
				{'userEnteredValue':linkcell['host']},
				{'userEnteredValue':linkcell['mode']},
				{'userEnteredValue':linkcell['test']},
				{'userEnteredValue':{'stringValue':e['line']}},
				{'userEnteredValue':{'numberValue':e['count']}},
				{'userEnteredValue':{'numberValue':e['tests']}},
				{'userEnteredValue':{'formulaValue':gsperc.format(e['tests'], test['resdetail']['tests'])}},
				{'userEnteredValue':html},
			]}
			s1data.append(r)
	s1data = [{'values':headrows[1]}] + \
		sorted(s1data, key=lambda k:gsissuesort(k), reverse=True)

	# Bugzilla tab
	if args.bugzilla:
		sBZdata = [{'values':headrows[4]}]
		for id in sorted(buglist, key=lambda k:(buglist[k]['worst'], int(k)), reverse=True):
			b = buglist[id]
			r = {'values':[
				{'userEnteredValue':{'formulaValue':gslink.format(b['url'], id)}},
				{'userEnteredValue':{'stringValue':b['desc']}},
				{'userEnteredValue':{'stringValue':''}},
				{'userEnteredValue':{'stringValue':''}},
				{'userEnteredValue':{'stringValue':''}},
				{'userEnteredValue':{'stringValue':''}},
				{'userEnteredValue':{'stringValue':''}},
				{'userEnteredValue':{'stringValue':''}},
			]}
			sBZdata.append(r)
			if 'match' not in buglist[id]:
				continue
			matches = buglist[id]['match']
			for m in sorted(matches, key=lambda k:(k['rate'], k['count'], k['host'], k['mode']), reverse=True):
				if m['testlink']:
					testlink = {'formulaValue':gslink.format(m['testlink'], m['testname'])}
				else:
					testlink = {'stringValue':m['testname']}
				if m['html'].startswith('http'):
					html = {'formulaValue':gslink.format(m['html'], 'html')}
				else:
					html = {'stringValue':m['html']}
				r = {'values':[
					{'userEnteredValue':{'stringValue':''}},
					{'userEnteredValue':{'stringValue':''}},
					{'userEnteredValue':{'stringValue':m['kernel']}},
					{'userEnteredValue':{'stringValue':m['host']}},
					{'userEnteredValue':testlink},
					{'userEnteredValue':{'numberValue':m['count']}},
					{'userEnteredValue':{'formulaValue':gsperc.format(m['count'], m['total'])}},
					{'userEnteredValue':html},
				]}
				sBZdata.append(r)

	# Suspend/Resume Devices tabs
	s23data = {}
	for type in sorted(deviceinfo, reverse=True):
		s23data[type] = [{'values':headrows[2]}]
		devlist = deviceinfo[type]
		for name in sorted(devlist, key=lambda k:devlist[k]['worst'], reverse=True):
			d = deviceinfo[type][name]
			url = op.join(urlprefix, d['url'])
			r = {'values':[
				{'userEnteredValue':{'stringValue':d['name']}},
				{'userEnteredValue':{'numberValue':float('%.3f' % d['average'])}},
				{'userEnteredValue':{'numberValue':d['count']}},
				{'userEnteredValue':{'numberValue':d['worst']}},
				{'userEnteredValue':{'stringValue':d['host']}},
				{'userEnteredValue':{'formulaValue':gslink.format(url, 'html')}},
			]}
			s23data[type].append(r)

	# Worst Suspend/Resume Devices tabs
	s45data = {'wsd':0, 'wrd':0}
	for entry in worst:
		s45data[entry] = [{'values':headrows[3]}]
		for dev in sorted(worst[entry], key=lambda k:worst[entry][k]['count'], reverse=True):
			r = {'values':[
				{'userEnteredValue':{'stringValue':dev}},
				{'userEnteredValue':{'numberValue':worst[entry][dev]['count']}},
			]}
			for h in hosts:
				r['values'].append({'userEnteredValue':{'numberValue':worst[entry][dev][h]}})
			s45data[entry].append(r)

	# create the spreadsheet
	data = {
		'properties': {
			'title': title
		},
		'sheets': [
			{
				'properties': {'sheetId': 0, 'title': 'Results'},
				'data': [
					{'startRow': 0, 'startColumn': 0, 'rowData': s0data}
				]
			},
			{
				'properties': {'sheetId': 1, 'title': 'Kernel Issues'},
				'data': [
					{'startRow': 0, 'startColumn': 0, 'rowData': s1data}
				]
			},
			{
				'properties': {'sheetId': 2, 'title': 'Suspend Devices'},
				'data': [
					{'startRow': 0, 'startColumn': 0, 'rowData': s23data['suspend']}
				]
			},
			{
				'properties': {'sheetId': 3, 'title': 'Resume Devices'},
				'data': [
					{'startRow': 0, 'startColumn': 0, 'rowData': s23data['resume']}
				]
			},
			{
				'properties': {'sheetId': 4, 'title': 'Worst Suspend Devices'},
				'data': [
					{'startRow': 0, 'startColumn': 0, 'rowData': s45data['wsd']}
				]
			},
			{
				'properties': {'sheetId': 5, 'title': 'Worst Resume Devices'},
				'data': [
					{'startRow': 0, 'startColumn': 0, 'rowData': s45data['wrd']}
				]
			},
		],
	}
	if args.bugzilla:
		data['sheets'].insert(2,
			{
				'properties': {'sheetId': 6, 'title': 'Bugzilla'},
				'data': [
					{'startRow': 0, 'startColumn': 0, 'rowData': sBZdata}
				]
			})

	pprint('building the spreadsheet')
	sheet = google_api_command('createsheet', data)
	if 'spreadsheetId' not in sheet:
		return False
	id = sheet['spreadsheetId']
	# format the spreadsheet
	fmt = {
		'requests': [
		{'repeatCell': {
			'range': {
				'sheetId': 0, 'startRowIndex': 1,
				'startColumnIndex': 9, 'endColumnIndex': 16,
			},
			'cell': {
				'userEnteredFormat': {
					'numberFormat': {'type': 'NUMBER', 'pattern': '0.00%;0%;0%'},
					'horizontalAlignment': 'RIGHT'
				}
			},
			'fields': 'userEnteredFormat.numberFormat,userEnteredFormat.horizontalAlignment'}},
		{'repeatCell': {
			'range': {
				'sheetId': 1, 'startRowIndex': 1,
				'startColumnIndex': 6, 'endColumnIndex': 7,
			},
			'cell': {
				'userEnteredFormat': {
					'numberFormat': {'type': 'NUMBER', 'pattern': '0.00%;0%;0%'},
					'horizontalAlignment': 'RIGHT'
				}
			},
			'fields': 'userEnteredFormat.numberFormat,userEnteredFormat.horizontalAlignment'}},
		{'repeatCell': {
			'range': {
				'sheetId': 0, 'startRowIndex': 1,
				'startColumnIndex': 16, 'endColumnIndex': 22,
			},
			'cell': {
				'userEnteredFormat': {'numberFormat': {'type': 'NUMBER', 'pattern': '0.000'}}
			},
			'fields': 'userEnteredFormat.numberFormat'}},
		{'autoResizeDimensions': {'dimensions': {'sheetId': 0,
			'dimension': 'COLUMNS', 'startIndex': 0, 'endIndex': 23}}},
		{'autoResizeDimensions': {'dimensions': {'sheetId': 1,
			'dimension': 'COLUMNS', 'startIndex': 0, 'endIndex': 8}}},
		{'autoResizeDimensions': {'dimensions': {'sheetId': 2,
			'dimension': 'COLUMNS', 'startIndex': 0, 'endIndex': 12}}},
		{'autoResizeDimensions': {'dimensions': {'sheetId': 3,
			'dimension': 'COLUMNS', 'startIndex': 0, 'endIndex': 12}}},
		{'autoResizeDimensions': {'dimensions': {'sheetId': 4,
			'dimension': 'COLUMNS', 'startIndex': 0, 'endIndex': 12}}},
		{'autoResizeDimensions': {'dimensions': {'sheetId': 5,
			'dimension': 'COLUMNS', 'startIndex': 0, 'endIndex': 12}}},
		]
	}

	if args.bugzilla:
		fmt['requests'].append([
			{'repeatCell': {
				'range': {
					'sheetId': 6, 'startRowIndex': 1,
					'startColumnIndex': 6, 'endColumnIndex': 7,
				},
				'cell': {
					'userEnteredFormat': {
						'numberFormat': {'type': 'NUMBER', 'pattern': '0.00%;0%;0%'},
							'horizontalAlignment': 'RIGHT'
				}
				},
				'fields': 'userEnteredFormat.numberFormat,userEnteredFormat.horizontalAlignment'}},
			{'autoResizeDimensions': {'dimensions': {'sheetId': 6,
				'dimension': 'COLUMNS', 'startIndex': 2, 'endIndex': 7}}},
			{'autoResizeDimensions': {'dimensions': {'sheetId': 6,
				'dimension': 'COLUMNS', 'startIndex': 0, 'endIndex': 1}}}
		])

	pprint('formatting the spreadsheet')
	response = google_api_command('formatsheet', id, fmt)
	pprint('{0} cells updated.'.format(len(response.get('replies'))));

	# move the spreadsheet into its proper folder
	pprint('moving the spreadsheet into its folder')
	file = google_api_command('move', id, kfid)
	pprint('spreadsheet id: %s' % id)
	if 'spreadsheetUrl' in sheet:
		pprint('SUCCESS: spreadsheet created -> %s' % sheet['spreadsheetUrl'])
	return True

def multiTestDesc(indir):
	desc = {'host':'', 'mode':'', 'kernel':'', 'sysinfo':''}
	dirs = re.split('/+', indir)
	if len(dirs) < 3:
		return desc
	m = re.match('suspend-(?P<m>[a-z]*)-[0-9]*-[0-9]*-[0-9]*min$', dirs[-1])
	if not m:
		return desc
	desc['kernel'] = dirs[-3]
	desc['host'] = dirs[-2]
	desc['mode'] = m.group('m')
	return desc

def pm_graph_report(args, indir, outpath, urlprefix, buglist, htmlonly):
	desc = multiTestDesc(indir)
	useturbo = usewifi = False
	issues = []
	testruns = []
	idx = total = begin = 0

	pprint('LOADING: %s' % indir)
	count = len(os.listdir(indir))
	# load up all the test data
	for dir in sorted(os.listdir(indir)):
		idx += 1
		if idx % 10 == 0 or idx == count:
			sys.stdout.write('\rLoading data... %.0f%%' % (100*idx/count))
			sys.stdout.flush()
		if not re.match('suspend-[0-9]*-[0-9]*$', dir) or not op.isdir(indir+'/'+dir):
			continue
		# create default entry for crash
		total += 1
		dt = datetime.strptime(dir, 'suspend-%y%m%d-%H%M%S')
		if not begin:
			begin = dt
		dirtime = dt.strftime('%Y/%m/%d %H:%M:%S')
		testfiles = {
			'html':'.*.html',
			'dmesg':'.*_dmesg.txt',
			'ftrace':'.*_ftrace.txt',
			'result': 'result.txt',
			'crashlog': 'dmesg-crash.log',
			'sshlog': 'sshtest.log',
		}
		data = {'mode': '', 'host': '', 'kernel': '',
			'time': dirtime, 'result': '',
			'issues': '', 'suspend': 0, 'resume': 0, 'sus_worst': '',
			'sus_worsttime': 0, 'res_worst': '', 'res_worsttime': 0,
			'url': dir, 'devlist': dict(), 'funclist': [] }
		# find the files and parse them
		found = dict()
		for file in os.listdir('%s/%s' % (indir, dir)):
			for i in testfiles:
				if re.match(testfiles[i], file):
					found[i] = '%s/%s/%s' % (indir, dir, file)

		if 'html' in found:
			# pass or fail, use html data
			hdata = sg.data_from_html(found['html'], indir, issues, True)
			if hdata:
				data = hdata
				data['time'] = dirtime
				# tests should all have the same kernel/host/mode
				for key in desc:
					if not desc[key] or len(testruns) < 1:
						desc[key] = data[key]
					elif desc[key] != data[key]:
						pprint('\nERROR:\n  Each test should have the same kernel, host, and mode\n'\
							'  In test folder %s/%s\n'\
							'  %s has changed from %s to %s, aborting...' % \
							(indir, dir, key.upper(), desc[key], data[key]))
						return False
			if not urlprefix:
				data['localfile'] = found['html']
		else:
			for key in desc:
				data[key] = desc[key]
		if 'pkgpc10' in data and 'syslpi' in data:
			useturbo = True
		if 'wifi' in data:
			usewifi = True
		netlost = False
		if 'sshlog' in found:
			fp = open(found['sshlog'])
			last = fp.read().strip().split('\n')[-1]
			if 'will issue an rtcwake in' in last or 'not responding' in last:
				netlost = True
		if netlost:
			data['issues'] =  'NETLOST' if not data['issues'] else 'NETLOST '+data['issues']
		if netlost and 'html' in found:
			match = [i for i in issues if i['match'] == 'NETLOST']
			if len(match) > 0:
				match[0]['count'] += 1
				if desc['host'] not in match[0]['urls']:
					match[0]['urls'][desc['host']] = [data['url']]
				elif data['url'] not in match[0]['urls'][desc['host']]:
					match[0]['urls'][desc['host']].append(data['url'])
			else:
				issues.append({
					'match': 'NETLOST', 'count': 1, 'urls': {desc['host']: [data['url']]},
					'line': 'NETLOST: network failed to recover after resume, needed restart to retrieve data',
				})
		if not data['result']:
			if netlost:
				data['result'] = 'hang'
			else:
				data['result'] = 'error'
		testruns.append(data)
	print('')
	if total < 1:
		pprint('ERROR: no folders matching suspend-%y%m%d-%H%M%S found')
		return False
	elif not desc['kernel'] or not desc['host'] or not desc['mode']:
		pprint('ERROR: all tests hung, cannot determine kernel/host/mode without data')
		return False
	# fill out default values based on test desc info
	desc['count'] = '%d' % len(testruns)
	desc['date'] = begin.strftime('%y%m%d')
	desc['time'] = begin.strftime('%H%M%S')
	desc['rc'] = kernelRC(desc['kernel'])
	out = outpath.format(**desc)
	for issue in issues:
		tests = 0
		for host in issue['urls']:
			tests += len(issue['urls'][host])
		issue['tests'] = tests

	# check the status of open bugs against this multitest
	bughtml, mybugs = '', []
	if len(buglist) > 0:
		mybugs = bz.bugzilla_check(buglist, desc, testruns, issues)
		bughtml = bz.html_table(testruns, mybugs, desc)

	# create the summary html files
	pprint('creating multitest html summary files')
	title = '%s %s %s' % (desc['host'], desc['kernel'], desc['mode'])
	sg.createHTMLSummarySimple(testruns,
		op.join(indir, 'summary.html'), title)
	sg.createHTMLIssuesSummary(testruns, issues,
		op.join(indir, 'summary-issues.html'), title, bughtml)
	devall = sg.createHTMLDeviceSummary(testruns,
		op.join(indir, 'summary-devices.html'), title)
	if htmlonly:
		pprint('SUCCESS: local summary html files updated')
		return True

	if len(testruns) < 1:
		pprint('NOTE: no valid test runs available, skipping spreadsheet')
		return False

	# create the summary google sheet
	pprint('creating multitest spreadsheet')
	outpath = op.dirname(out)
	pid = gdrive_mkdir(outpath)
	file = createSpreadsheet(testruns, devall, issues, mybugs, outpath,
		urlprefix, op.basename(out), useturbo, usewifi)
	pprint('SUCCESS: spreadsheet created -> %s' % file)
	return True

def genHtml(subdir, count=0, force=False):
	sv = sg.sysvals
	cmds = []
	sgcmd = 'sleepgraph'
	if sys.argv[0].endswith('googlesheet.py'):
		sgcmd = op.abspath(sys.argv[0]).replace('googlesheet.py', 'sleepgraph.py')
	cexec = sys.executable+' '+sgcmd
	for dirname, dirnames, filenames in os.walk(subdir):
		sv.dmesgfile = sv.ftracefile = sv.htmlfile = ''
		for filename in filenames:
			if(re.match('.*_dmesg.txt', filename)):
				sv.dmesgfile = op.join(dirname, filename)
			elif(re.match('.*_ftrace.txt', filename)):
				sv.ftracefile = op.join(dirname, filename)
		sv.setOutputFile()
		if sv.ftracefile and sv.htmlfile and \
			(force or not op.exists(sv.htmlfile)):
			if sv.dmesgfile:
				cmd = '%s -dmesg %s -ftrace %s -dev' % \
					(cexec, sv.dmesgfile, sv.ftracefile)
			else:
				cmd = '%s -ftrace %s -dev' % (cexec, sv.ftracefile)
			cmds.append(cmd)
	if len(cmds) < 1:
		return
	mp = MultiProcess(cmds, 600)
	mp.run(count)

def find_multitests(folder, urlprefix):
	# search for stress test output folders with at least one test
	pprint('searching folder for multitest data')
	multitests = []
	for dirname, dirnames, filenames in os.walk(folder, followlinks=True):
		for dir in dirnames:
			if re.match('suspend-[0-9]*-[0-9]*$', dir):
				r = op.relpath(dirname, folder)
				if urlprefix:
					urlp = urlprefix if r == '.' else op.join(urlprefix, r)
				else:
					urlp = ''
				multitests.append((dirname, urlp))
				break
	if len(multitests) < 1:
		doError('no folders matching suspend-%y%m%d-%H%M%S found')
	pprint('%d multitest folders found' % len(multitests))
	return multitests

def generate_test_timelines(args, multitests):
	pprint('GENERATING SLEEPGRAPH TIMELINES')
	sg.sysvals.usedevsrc = True
	for testinfo in multitests:
		indir, urlprefix = testinfo
		if args.parallel >= 0:
			genHtml(indir, args.parallel, args.regenhtml)
		else:
			sg.genHtml(indir, args.regenhtml)

def generate_test_spreadsheets(args, multitests, buglist):
	if args.parallel < 0 or len(multitests) < 2:
		for testinfo in multitests:
			indir, urlprefix = testinfo
			pm_graph_report(args, indir, args.tpath, urlprefix, buglist, args.htmlonly)
		return
	# multiprocess support, requires parallel arg and multiple tests
	cexec = sys.executable+' '+op.abspath(sys.argv[0])
	if args.htmlonly:
		cexec += ' -htmlonly'
	if args.bugzilla:
		fp = NamedTemporaryFile(delete=False)
		pickle.dump(buglist, fp)
		fp.close()
		cmdhead = '%s -bugfile %s -create test -tpath "%s"' % (cexec, fp.name, args.tpath)
	else:
		cmdhead = '%s -create test -tpath "%s"' % (cexec, args.tpath)
	cmds = []
	for testinfo in multitests:
		indir, urlprefix = testinfo
		cmds.append(cmdhead + ' -urlprefix "{0}" {1}'.format(urlprefix, indir))
	mp = MultiProcess(cmds, 86400)
	mp.run(args.parallel)
	if op.exists(fp.name):
		os.remove(fp.name)

def generate_summary_spreadsheet(args, multitests, buglist):
	global deviceinfo

	# clear the global data on each high level summary
	deviceinfo = {'suspend':dict(),'resume':dict()}
	for id in buglist:
		if 'match' in buglist[id]:
			del buglist[id]['match']
		for item in ['matches', 'worst']:
			if item in buglist[id]:
				buglist[id][item] = 0

	pprint('loading multitest html summary files')
	data = []
	for testinfo in multitests:
		indir, urlprefix = testinfo
		file = op.join(indir, 'summary.html')
		if op.exists(file):
			info(file, data, args)
	if len(data) < 1:
		return False

	for type in sorted(deviceinfo, reverse=True):
		for name in deviceinfo[type]:
			d = deviceinfo[type][name]
			d['average'] = d['total'] / d['count']

	if args.bugzilla:
		pprint('scanning the data for bugzilla issues')
		summarizeBuglist(args, data, buglist)

	pprint('creating %s summary' % args.stype)
	if args.stype == 'sheet':
		createSummarySpreadsheet(args, data, deviceinfo, buglist)
		if not args.mail:
			return True
		pprint('creating html summary to mail')
		out = html_output(args, data, buglist)
	elif args.stype == 'html':
		out = html_output(args, data, buglist)
	else:
		out = text_output(args, data, buglist)

	if args.mail:
		pprint('sending output via email')
		server, sender, receiver, subject = args.mail
		type = 'text/html' if args.stype in ['html', 'sheet'] else 'text'
		send_mail(server, sender, receiver, type, subject, out)
	elif args.spath == 'stdout':
		pprint(out)
	else:
		file = gdrive_path(args.spath, data[0])
		dir = op.dirname(file)
		if dir and not op.exists(dir):
			os.makedirs(dir)
		fp = open(file, 'w')
		fp.write(out)
		fp.close()
	return True

def folder_as_tarball(args):
	res = call('tar -tzf %s > /dev/null 2>&1' % args.folder, shell=True)
	if res != 0:
		doError('%s is not a tarball(gz) or a folder' % args.folder, False)
	if not args.webdir:
		doError('you must supply a -webdir when processing a tarball')
	tdir, tball = mkdtemp(prefix='sleepgraph-multitest-data-'), args.folder
	pprint('Extracting tarball to %s...' % tdir)
	call('tar -C %s -xvzf %s > /dev/null' % (tdir, tball), shell=True)
	args.folder = tdir
	if not args.rmtar:
		return [tdir]
	return [tdir, tball]

def sort_and_copy(args, multitestdata):
	multitests, kernels = [], []
	if not args.webdir:
		return (multitests, kernels)
	for testinfo in multitestdata:
		indir, urlprefix = testinfo
		data, html = False, ''
		for dir in sorted(os.listdir(indir)):
			if not re.match('suspend-[0-9]*-[0-9]*$', dir) or not op.isdir(indir+'/'+dir):
				continue
			for file in os.listdir('%s/%s' % (indir, dir)):
				if not file.endswith('.html'):
					continue
				html = '%s/%s/%s' % (indir, dir, file)
				data = sg.data_from_html(html, indir, [], False)
				if data:
					break
			if data:
				break
		if not data or 'kernel' not in data or 'host' not in data or \
			'mode' not in data or 'time' not in data:
			continue
		try:
			dt = datetime.strptime(data['time'], '%Y/%m/%d %H:%M:%S')
		except:
			continue
		kernel, host, test = data['kernel'], data['host'], op.basename(indir)
		if not re.match('^suspend-.*-[0-9]{6}-[0-9]{6}.*', test):
			test = 'suspend-%s-%s-multi' % (data['mode'], dt.strftime('%y%m%d-%H%M%S'))
		kdir = op.join(args.webdir, kernel)
		if not op.exists(kdir):
			if args.datadir and args.datadir != args.webdir:
				ksrc = op.join(args.datadir, kernel)
				if not op.exists(ksrc):
					os.makedirs(ksrc)
				os.symlink(ksrc, kdir)
			else:
				os.makedirs(kdir)
		elif not op.isdir(kdir):
			pprint('WARNING: %s is a file (should be dir), skipping %s ...' % (kdir, indir))
			continue
		outdir = op.join(args.webdir, kernel, host, test)
		if not op.exists(outdir):
			try:
				os.makedirs(outdir)
			except:
				pprint('WARNING: failed to make %s, skipping %s ...' % (outdir, indir))
				continue
		copy_tree(indir, outdir)
		if args.urlprefix:
			urlprefix = op.join(args.urlprefix, op.relpath(outdir, args.webdir))
		if kernel not in kernels:
			kernels.append(kernel)
		multitests.append((outdir, urlprefix))
	return (multitests, kernels)

def rcsort(args, kernels):
	rclist, rchash = [], dict()
	for dirname in sorted(os.listdir(args.webdir)):
		dir = op.join(args.webdir, dirname)
		if not op.isdir(dir):
			continue
		rc = kernelRC(dirname, True)
		if not rc:
			continue
		if op.islink(dir):
			dir = op.realpath(dir)
		if dirname in kernels and rc not in rclist:
			rclist.append(rc)
		if rc not in rchash:
			rchash[rc] = []
		rchash[rc].append((dir, dirname))
	for rc in sorted(rchash):
		rcdir = op.join(args.rcdir, rc)
		if not op.exists(rcdir):
			os.mkdir(rcdir)
		for dir, kernel in rchash[rc]:
			link = op.join(rcdir, kernel)
			if op.exists(link):
				continue
			os.symlink(dir, link)
	return rclist

def doError(msg, help=False):
	global trash
	if(help == True):
		printHelp()
	empty_trash()
	pprint('ERROR: %s\n' % msg)
	sys.exit(1)

def printHelp():
	pprint('\nGoogle Sheet Summary Utility\n'\
	'  Summarize sleepgraph multitests in the form of googlesheets.\n'\
	'  This tool searches a dir for sleepgraph multitest folders and\n'\
	'  generates google sheet summaries for them. It can create individual\n'\
	'  summaries of each test and a high level summary of all tests found.\n'\
	'\nUsage: googlesheet.py <options> indir\n'\
	'Options:\n'\
	'  -tpath path\n'\
	'      The pathname of the test spreadsheet(s) to be created on google drive.\n'\
	'      Variables are {kernel}, {host}, {mode}, {count}, {date}, {time}.\n'\
	'      default: "pm-graph-test/{kernel}/{host}/sleepgraph-{date}-{time}-{mode}-x{count}"\n'\
	'  -spath path\n'\
	'      The pathname of the summary to be created on google or local drive.\n'\
	'      default: "pm-graph-test/{kernel}/summary_{kernel}"\n'\
	'  -stype value\n'\
	'      Type of summary file to create, text/html/sheet (default: sheet).\n'\
	'      sheet: created on google drive, text/html: created on local drive\n'\
	'  -create value\n'\
	'      What output(s) should the tool create: test/summary/both (default: test).\n'\
	'      test: create the test spreadsheet(s) for each multitest run found.\n'\
	'      summary: create the high level summary of all multitests found.\n'\
	'  -urlprefix url\n'\
	'      The URL prefix to use to link to each html timeline (default: blank)\n'\
	'      Without this arg the timelines are gzipped and uploaded to google drive.\n'\
	'      For links to work the "indir" folder must be exposed via a web server.\n'\
	'      The urlprefix should be the web visible link to the "indir" contents.\n'\
	'  -bugzilla\n'\
	'      Load a collection of bugzilla issues and check each timeline to see\n'\
	'      if they match the requirements and fail or pass. The output of this is\n'\
	'      a table in the issues summary, and bugzilla tabs in the google sheets.\n'\
	'  -mail server sender receiver subject\n'\
	'      Send the summary out via email, only works for -stype text/html\n'\
	'      The html mail will include links to the google sheets that exist\n'\
	'Advanced:\n'\
	'  -genhtml\n'\
	'      Regenerate any missing html for the sleepgraph runs found.\n'\
	'      This is useful if you ran sleepgraph with the -skiphtml option.\n'\
	'  -regenhtml\n'\
	'      Regenerate all html for the sleepgraph runs found, overwriting the old\n'\
	'      html. This is useful if you have a new version of sleepgraph.\n'\
	'  -htmlonly\n'\
	'      Only generate html files. i.e. summary.html, summary-devices.html,\n'\
	'      summary-issues.html, and any timelines found with -genhtml or -regenhtml.\n'\
	'  -parallel count\n'\
	'      Multi-process the googlesheet and html timelines with up to N processes\n'\
	'      at once. N=0 means use cpu count. Default behavior is one at a time.\n'\
	'Initial Setup:\n'\
	'  -setup                     Enable access to google drive apis via your account\n'\
	'  --noauth_local_webserver   Dont use local web browser\n'\
	'    example: "./googlesheet.py -setup --noauth_local_webserver"\n'\
	'Utility Commands:\n'\
	'  -gid gpath      Get the gdrive id for a given file/folder (used to test setup)\n', False)
	return True

# ----------------- MAIN --------------------
# exec start (skipped if script is loaded as library)
if __name__ == '__main__':
	user = "" if 'USER' not in os.environ else os.environ['USER']

	# handle help, setup, and utility commands separately
	if len(sys.argv) < 2:
		printHelp()
		sys.exit(1)
	args = iter(sys.argv[1:])
	for arg in args:
		if(arg in ['-h', '--help']):
			printHelp()
			sys.exit(0)
		elif(arg == '-gid'):
			try:
				val = next(args)
			except:
				doError('No gpath supplied', True)
			initGoogleAPIs()
			out = gdrive_find(val)
			if out:
				pprint(out)
				sys.exit(0)
			pprint('File not found on google drive')
			sys.exit(1)
		elif(arg == '-backup'):
			try:
				val = next(args)
			except:
				doError('No gpath supplied', True)
			initGoogleAPIs()
			dir, title = op.dirname(val), op.basename(val)
			gdrive_backup(dir, title)
			sys.exit(0)
		elif(arg == '-setup'):
			sys.exit(setupGoogleAPIs())

	# use argparse to handle normal operation
	parser = argparse.ArgumentParser()
	parser.add_argument('-tpath', metavar='filepath',
		default='pm-graph-test/{kernel}/{host}/sleepgraph-{date}-{time}-{mode}-x{count}')
	parser.add_argument('-spath', metavar='filepath',
		default='pm-graph-test/{kernel}/summary_{kernel}')
	parser.add_argument('-stype', metavar='value',
		choices=['text', 'html', 'sheet'], default='sheet')
	parser.add_argument('-create', metavar='value',
		choices=['test', 'summary', 'both'], default='test')
	parser.add_argument('-mail', nargs=4, metavar=('server', 'sender', 'receiver', 'subject'))
	parser.add_argument('-genhtml', action='store_true')
	parser.add_argument('-regenhtml', action='store_true')
	parser.add_argument('-bugzilla', action='store_true')
	parser.add_argument('-urlprefix', metavar='url', default='')
	parser.add_argument('-parallel', metavar='count', type=int, default=-1)
	parser.add_argument('-htmlonly', action='store_true')
	# hidden arguments for testing only
	parser.add_argument('-bugtest', metavar='file')
	parser.add_argument('-bugfile', metavar='file')
	parser.add_argument('-webdir', metavar='folder')
	parser.add_argument('-datadir', metavar='folder')
	parser.add_argument('-rcdir', metavar='folder')
	parser.add_argument('-rmtar', action='store_true')
	# required positional arguments
	parser.add_argument('folder')
	args = parser.parse_args()
	tarball, kernels = False, []

	for dir in [args.webdir, args.datadir, args.rcdir]:
		if not dir:
			continue
		if not op.exists(dir) or not op.isdir(dir):
			doError('%s does not exist' % dir, False)

	if not op.exists(args.folder):
		doError('%s does not exist' % args.folder, False)

	if not op.isdir(args.folder):
		tarball = True
		trash = folder_as_tarball(args)

	if args.urlprefix and args.urlprefix[-1] == '/':
		args.urlprefix = args.urlprefix[:-1]

	# get the buglist data
	buglist = dict()
	if args.bugzilla or args.bugtest:
		pprint('Loading open bugzilla issues')
		if args.bugtest:
			args.bugzilla = True
			buglist = bz.loadissue(args.bugtest)
		else:
			buglist = bz.pm_stress_test_issues()
	elif args.bugfile:
		pprint('Loading open bugzilla issues from file')
		if not op.exists(args.bugfile):
			doError('%s does not exist' % args.bugfile, False)
		args.bugzilla = True
		buglist = pickle.load(open(args.bugfile, 'rb'))

	# get the multitests from the folder
	multitests = find_multitests(args.folder, args.urlprefix)

	# regenerate any missing timlines
	if args.genhtml or args.regenhtml:
		generate_test_timelines(args, multitests)

	# sort and copy data from the tarball location
	if tarball:
		multitests, kernels = sort_and_copy(args, multitests)

	# initialize google apis if we will need them
	if args.htmlonly:
		args.stype = 'html' if args.stype == 'sheet' else args.stype
	else:
		initGoogleAPIs()

	# generate the individual test summary html and/or sheets
	if args.create in ['test', 'both']:
		if args.htmlonly:
			pprint('CREATING MULTITEST SUMMARY HTML FILE')
		else:
			pprint('CREATING MULTITEST SUMMARY GOOGLESHEET')
		generate_test_spreadsheets(args, multitests, buglist)
	if args.create == 'test':
		empty_trash()
		sys.exit(0)

	# generate the high level summary(s) for the test data
	if tarball:
		urlprefix = args.urlprefix
		for kernel in kernels:
			pprint('CREATING SUMMARY FOR KERNEL %s' % kernel)
			args.folder = op.join(args.webdir, kernel)
			args.urlprefix = op.join(urlprefix, kernel)
			multitests = find_multitests(args.folder, args.urlprefix)
			if not generate_summary_spreadsheet(args, multitests, buglist):
				pprint('WARNING: no summary for kernel %s' % kernel)
		if args.rcdir:
			args.spath = op.join(op.dirname(op.dirname(args.spath)), '{rc}_summary')
			args.urlprefix = urlprefix
			rcs = rcsort(args, kernels)
			for rc in rcs:
				pprint('CREATING SUMMARY FOR RELEASE CANDIDATE %s' % rc)
				args.folder = op.join(args.rcdir, rc)
				multitests = find_multitests(args.folder, args.urlprefix)
				if not generate_summary_spreadsheet(args, multitests, buglist):
					pprint('WARNING: no summary for RC %s' % rc)
	else:
		generate_summary_spreadsheet(args, multitests, buglist)
	empty_trash()
	pprint('GOOGLESHEET SUCCESSFULLY COMPLETED')
