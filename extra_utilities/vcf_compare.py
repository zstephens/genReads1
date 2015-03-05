#!/usr/bin/env python
# encoding: utf-8

""" **************************************************

vcf_compare.py

- compare vcf file produced by workflow to golden vcf produced by simulator

Written by:		Zach Stephens
Date:			January 20, 2015
Contact:		zstephe2@illinois.edu

************************************************** """

import sys
import os
import copy
import time
import bisect
import re
import numpy as np
import optparse

#
#	mappability track functions
#
EIGHTBIT     = {}
REV_EIGHTBIT = {}
for i in xrange(256):
	key = bytearray('\0\0\0\0\0\0\0\0')
	if i&128: key[0] = '\1'
	if i&64: key[1] = '\1'
	if i&32: key[2] = '\1'
	if i&16: key[3] = '\1'
	if i&8: key[4] = '\1'
	if i&4: key[5] = '\1'
	if i&2: key[6] = '\1'
	if i&1: key[7] = '\1'
	EIGHTBIT[str(key)]   = chr(i)
	REV_EIGHTBIT[chr(i)] = key

def compressTrack(t):
	len_t = len(t)
	padded = len_t-(len_t%8)+8
	while len(t) != padded:
		t.append('\0')
	outStr = ''
	for i in xrange(0,padded,8):
		outStr += EIGHTBIT[str(t[i:i+8])]
	return (len_t,outStr.encode("zlib"))

def decompressTrack(ct):
	outBA = bytearray()
	dc = ct[1].decode("zlib")
	for i in xrange(len(dc)):
		outBA.extend(REV_EIGHTBIT[dc[i]])
	return outBA[:ct[0]]

#
#
#

EV_BPRANGE = 50		# how far to either side of a particular variant location do we want to check for equivalents?

DEFAULT_QUAL = -666	# if we can't find a qual score, use this instead so we know it's missing

MAX_VAL = 9999999999999	# an unreasonably large value that no reference fasta could concievably be longer than

DESC   = """%prog: vcf comparison script."""
VERS   = 0.1

PARSER = optparse.OptionParser('python %prog [options] -r <ref.fa> -g <golden.vcf> -w <workflow.vcf>',description=DESC,version="%prog v"+str(VERS))

PARSER.add_option('-r', help='* Reference Fasta', dest='REFF', action='store', metavar='<ref.fa>')
PARSER.add_option('-g', help='* Golden VCF',      dest='GVCF', action='store', metavar='<golden.vcf>')
PARSER.add_option('-w', help='* Workflow VCF',    dest='WVCF', action='store', metavar='<workflow.vcf>')
PARSER.add_option('-o', help='* Output Prefix',   dest='OUTF', action='store', metavar='<prefix>')
PARSER.add_option('-m', help='Mappability Track', dest='MTRK', action='store', metavar='<tracks.dat>')
PARSER.add_option('-t', help='Targetted Regions', dest='TREG', action='store', metavar='<regions.bed>')
PARSER.add_option('-T', help='Min Region Len',    dest='MTRL', action='store', metavar='<int>')
PARSER.add_option('-c', help='Coverage Filter Threshold [%default]',       dest='DP_THRESH', default=15, action='store', metavar='<int>')
PARSER.add_option('-a', help='Allele Freq Filter Threshold [%default]',    dest='AF_THRESH', default=0.3, action='store', metavar='<float>')

PARSER.add_option('--vcf-out',   help="Output Match/FN/FP variants [%default]",       dest='VCF_OUT', default=False, action='store_true')
PARSER.add_option('--no-plot',   help="No plotting [%default]",                       dest='NO_PLOT', default=False, action='store_true')
PARSER.add_option('--incl-homs', help="Include homozygous ref calls [%default]",      dest='INCL_H',  default=False, action='store_true')
PARSER.add_option('--incl-fail', help="Include calls that failed filters [%default]", dest='INCL_F',  default=False, action='store_true')
PARSER.add_option('--fast',      help="No equivalent variant detection [%default]",   dest='FAST',    default=False, action='store_true')

(OPTS,ARGS) = PARSER.parse_args()

REFERENCE    = OPTS.REFF
GOLDEN_VCF   = OPTS.GVCF
WORKFLOW_VCF = OPTS.WVCF
OUT_PREFIX   = OPTS.OUTF
MAPTRACK     = OPTS.MTRK
BEDFILE      = OPTS.TREG
DP_THRESH    = int(OPTS.DP_THRESH)
AF_THRESH    = float(OPTS.AF_THRESH)

VCF_OUT      = OPTS.VCF_OUT
NO_PLOT      = OPTS.NO_PLOT
INCLUDE_HOMS = OPTS.INCL_H
INCLUDE_FAIL = OPTS.INCL_F
FAST         = OPTS.FAST

if OPTS.MTRL != None:
	MINREGIONLEN = int(OPTS.MTRL)
else:
	MINREGIONLEN = None

if REFERENCE == None:
	print 'Error: No reference provided.'
	exit(1)
if GOLDEN_VCF == None:
	print 'Error: No golden VCF provided.'
	exit(1)
if WORKFLOW_VCF == None:
	print 'Error: No workflow VCF provided.'
	exit(1)
if OUT_PREFIX == None:
	print 'Error: No output prefix provided.'
	exit(1)
if (BEDFILE != None and MINREGIONLEN == None) or (BEDFILE == None and MINREGIONLEN != None):
	print 'Error: Both -t and -T must be specified'
	exit(1)

if NO_PLOT == False:
	import matplotlib
	matplotlib.use('Agg')
	import matplotlib.pyplot as mpl
	from matplotlib_venn import venn2, venn3
	import warnings
	warnings.filterwarnings("ignore", category=UserWarning, module='matplotlib_venn')

AF_STEPS = 20
AF_KEYS  = np.linspace(0.0,1.0,AF_STEPS+1)

def quantize_AF(af):
	if af >= 1.0:
		return AF_STEPS
	elif af <= 0.0:
		return 0
	else:
		return int(af*AF_STEPS)

colDict = {}	# [col_name] = col_index
colSamp = []	# list of indices of columns containing info fields for each sample present

VCF_HEADER = '##fileformat=VCFv4.1\n##reference='+REFERENCE+'##INFO=<ID=DP,Number=1,Type=Integer,Description="Total Depth">\n##INFO=<ID=AF,Number=A,Type=Float,Description="Allele Frequency">\n'

DP_TOKENS = ['DP','DPU','DPI']	# in the order that we'll look for them

def parseLine(splt):

	#	check if we want to proceed..
	ra = splt[colDict['REF']]
	aa = splt[colDict['ALT']]
	if not(INCLUDE_HOMS) and (aa == '.' or aa == '' or aa == ra):
		return None
	if not(INCLUDE_FAIL) and (splt[colDict['FILTER']] != 'PASS' and splt[colDict['FILTER']] != '.'):
		return None

	#	default vals
	cov  = None
	qual = DEFAULT_QUAL
	alt_alleles = []
	alt_freqs   = [None]

	#	any alt alleles?
	alt_split = aa.split(',')
	if len(alt_split) > 1:
		alt_alleles = alt_split

	#	cov
	for dp_tok in DP_TOKENS:
		#	check INFO for DP first
		if 'INFO' in colDict and dp_tok+'=' in splt[colDict['INFO']]:
			cov = int(re.findall(re.escape(dp_tok)+r"=[0-9]+",splt[colDict['INFO']])[0][3:])
		#	check FORMAT/SAMPLE for DP second:
		elif 'FORMAT' in colDict and len(colSamp):
			format = splt[colDict['FORMAT']]+':'
			if ':'+dp_tok+':' in format:
				dpInd = format.split(':').index(dp_tok)
				cov   = int(splt[colSamp[0]].split(':')[dpInd])
		if cov != None:
			break

	#	check INFO for AF first
	af = None
	if 'INFO' in colDict and 'AF=' in splt[colDict['INFO']]:
		info = splt[colDict['INFO']]+';'
		af  = re.findall(r"AF=.*?(?=;)",info)[0][3:]
	#	check FORMAT/SAMPLE for AF second:
	elif 'FORMAT' in colDict and len(colSamp):
		format = splt[colDict['FORMAT']]+':'
		if ':AF:' in format:
			afInd = splt[colDict['FORMAT']].split(':').index('AF')
			af    = splt[colSamp[0]].split(':')[afInd]
	if af != None:
		af_splt = af.split(',')
		while(len(af_splt) < len(alt_alleles)):	# are we lacking enough AF values for some reason?
			af_splt.append(af_splt[-1])			# phone it in.
		if len(af_splt) != 0 and af_splt[0] != '.' and af_splt[0] != '':		# missing data, yay
			alt_freqs = [float(n) for n in af_splt]
	else:
		alt_freqs = [None]*max([len(alt_alleles),1])

	#	get QUAL if it's interesting
	if 'QUAL' in colDict and splt[colDict['QUAL']] != '.':
		qual = float(splt[colDict['QUAL']])

	return (cov, qual, alt_alleles, alt_freqs)

#
#
#
def parseVCF(VCF_FILENAME,refName,targRegionsFl,outFile,outBool):
	v_Hashed   = {}
	v_Alts     = {}
	v_Cov      = {}
	v_AF       = {}
	v_Qual     = {}
	v_TargLen  = {}
	nBelowMinRLen   = 0
	line_unique     = 0
	colDict = {}
	for line in open(VCF_FILENAME,'r'):
		if line[0] != '#':
			if len(colDict) == 0:
				print '\n\nError: VCF has no header?\n'+VCF_FILENAME+'\n\n'
				exit(1)
			splt = line.split('\t')
			if splt[0] == refName:

				var  = (int(splt[1]),splt[3],splt[4])
				targInd = bisect.bisect(targRegionsFl,var[0])

				if targInd%2 == 1:
					targLen = targRegionsFl[targInd]-targRegionsFl[targInd-1]
					if (BEDFILE != None and targLen >= MINREGIONLEN) or BEDFILE == None:
						
						pl_out = parseLine(splt)
						if pl_out == None:
							continue
						(cov, qual, aa, af) = pl_out

						if var not in v_Hashed:
							areWeUnique = False
							if len(aa):
								allVars = [(var[0],var[1],n) for n in aa]
								for i in xrange(len(allVars)):
									if allVars[i] in v_Hashed:
										continue
									areWeUnique = True
									v_Hashed[allVars[i]] = 1
									v_Alts[allVars[i]]  = allVars
							else:
								if var in v_Hashed:
									continue
								areWeUnique = True
								v_Hashed[var] = 1

							if areWeUnique:
								if cov != None:
									v_Cov[var] = cov
								v_AF[var]      = af[0]		# only use first AF, even if multiple. fix this later?
								v_Qual[var]    = qual
								v_TargLen[var] = targLen
								line_unique += 1
						else:
							continue

					else:
						nBelowMinRLen += 1
		else:
			if line[1] != '#':
				cols = line[1:-1].split('\t')
				for i in xrange(len(cols)):
					if 'FORMAT' in colDict:
						colSamp.append(i)
					colDict[cols[i]] = i
				if VCF_OUT and outBool:
					outBool = False
					outFile.write(line)

	return (v_Hashed, v_Alts, v_Cov, v_AF, v_Qual, v_TargLen, nBelowMinRLen, line_unique)


def condenseAlts(listIn,altsList):
	to_condense   = {}
	ext_info_dict = {}
	for i in xrange(len(listIn)):
		var = listIn[i]
		if var in altsList:
			concat = (var[0],var[1],','.join([n[2] for n in altsList[var]]))
			if concat not in to_condense:
				to_condense[concat] = []
			to_condense[concat].append(i)
	delList = [j for i in to_condense.values() for j in i]
	outList = [listIn[i] for i in xrange(len(listIn)) if i not in delList]
	for n in to_condense.keys():
		outList.append(n)
	return outList


def main():

	ref = []
	f = open(REFERENCE,'r')
	nLines = 0
	prevR = None
	prevP = None
	#ref_inds = []
	#sys.stdout.write('\nindexing reference fasta... ')
	#sys.stdout.flush()
	#tt = time.time()
	#while 1:
	#	nLines += 1
	#	data = f.readline()
	#	if not data:
	#		ref_inds.append( (prevR, prevP, f.tell()-len(data)) )
	#		break
	#	if data[0] == '>':
	#		if prevP != None:
	#			ref_inds.append( (prevR, prevP, f.tell()-len(data)) )
	#		prevP = f.tell()
	#		prevR = data[1:-1]
	#print '{0:.3f} (sec)'.format(time.time()-tt)
	ref_inds = [('chrM', 6, 16909), ('chr1', 16915, 254252549), ('chr2', 254252555, 502315916), ('chr3', 502315922, 704298801), ('chr4', 704298807, 899276169), ('chr5', 899276175, 1083809741), ('chr6', 1083809747, 1258347116), ('chr7', 1258347122, 1420668559), ('chr8', 1420668565, 1569959868), ('chr9', 1569959874, 1713997574), ('chr10', 1713997581, 1852243023), ('chr11', 1852243030, 1989949677), ('chr12', 1989949684, 2126478617), ('chr13', 2126478624, 2243951900), ('chr14', 2243951907, 2353448438), ('chr15', 2353448445, 2458030465), ('chr16', 2458030472, 2550192321), ('chr17', 2550192328, 2633011443), ('chr18', 2633011450, 2712650243), ('chr19', 2712650250, 2772961813), ('chr20', 2772961820, 2837247851), ('chr21', 2837247858, 2886340351), ('chr22', 2886340358, 2938671016), ('chrX', 2938671022, 3097046994), ('chrY', 3097047000, 3157608038)]

	ztV = 0
	ztW = 0
	znP = 0
	zfP = 0
	znF = 0
	znE = 0
	if BEDFILE != None:
		zbM = 0

	mappability_vs_FN = {0:0, 1:0}	# [0] = # of FNs that were in mappable regions, [1] = # of FNs that were in unmappable regions
	coverage_vs_FN    = {}			# [C] = # of FNs that were covered by C reads
	alleleBal_vs_FN   = {}			# [AF] = # of FNs that were heterozygous genotypes with allele freq AF (quantized to multiples of 1/AF_STEPS)
	for n in AF_KEYS:
		alleleBal_vs_FN[n] = 0

	#
	#	read in mappability track
	#
	mappability_tracks = {}
	if MAPTRACK != None:
		sys.stdout.write('reading mappability tracks... ')
		sys.stdout.flush()
		tt = time.time()
		mapf = open(MAPTRACK,"rb")
		[headerLen] = np.fromfile(mapf,'<i4',1)
		header = mapf.read(headerLen)
		names  = []
		lens   = []
		bytes  = []
		for line in header.split('\n')[:-1]:
			splt = line.split('\t')
			names.append(splt[0])
			lens.append(int(splt[1]))
			bytes.append(int(splt[2]))

		for i in xrange(len(names)):
			mappability_tracks[names[i]] = decompressTrack((lens[i],mapf.read(bytes[i])))
		print '{0:.3f} (sec)'.format(time.time()-tt)

	#
	#	init vcf output, if desired
	#
	vcfo2 = None
	vcfo3 = None
	global vcfo2_firstTime
	global vcfo3_firstTime
	vcfo2_firstTime = False
	vcfo3_firstTime = False
	if VCF_OUT:
		vcfo2 = open(OUT_PREFIX+'_FN.vcf','w')
		vcfo3 = open(OUT_PREFIX+'_FP.vcf','w')
		vcfo2_firstTime = True
		vcfo3_firstTime = True

	#
	#	data for plotting FN analysis
	#
	set1 = []
	set2 = []
	set3 = []
	varAdj = 0

	#
	#
	#	For each sequence in reference fasta...
	#
	#
	for n_RI in ref_inds:

		refName = n_RI[0]
		if FAST == False:
			f.seek(n_RI[1])
			print 'reading '+refName+'...',
			myDat  = f.read(n_RI[2]-n_RI[1]).split('\n')
			myLen  = sum([len(m) for m in myDat])
			if sys.version_info >= (2,7):
				print '{:,} bp'.format(myLen)
			else:
				print '{0:} bp'.format(myLen)
			inWidth = len(myDat[0])
			if len(myDat[-1]) == 0:	# if last line is empty, remove it.
				del myDat[-1]
			if inWidth*(len(myDat)-1)+len(myDat[-1]) != myLen:
				print 'fasta column-width not consistent.'
				print myLen, inWidth*(len(myDat)-1)+len(myDat[-1])
				for i in xrange(len(myDat)):
					if len(myDat[i]) != inWidth:
						print i, len(myDat[i]), inWidth
				exit(1)

			myDat = bytearray(''.join(myDat)).upper()
			myLen = len(myDat)

		#
		#	Parse relevant targeted regions
		#
		targRegionsFl = []
		if BEDFILE != None:
			bedfile = open(BEDFILE,'r')
			for line in bedfile:
				splt = line.split('\t')
				if splt[0] == refName:
					targRegionsFl.extend((int(splt[1]),int(splt[2])))
			bedfile.close()
		else:
			targRegionsFl = [-1,MAX_VAL+1]

		#
		#	Parse vcf files
		#
		sys.stdout.write('comparing variation in '+refName+'... ')
		sys.stdout.flush()
		tt = time.time()

		global colDict
		(correctHashed, correctAlts, correctCov, correctAF, correctQual, correctTargLen, correctBelowMinRLen, correctUnique)        = parseVCF(GOLDEN_VCF, refName, targRegionsFl, vcfo2, vcfo2_firstTime)
		(workflowHashed, workflowAlts, workflowCov, workflowAF, workflowQual, workflowTarLen, workflowBelowMinRLen, workflowUnique) = parseVCF(WORKFLOW_VCF, refName, targRegionsFl, vcfo3, vcfo3_firstTime)

		print '\nRAWRG:',len(correctHashed)
		print 'RAWRW:',len(workflowHashed)

		#
		#	Deduce which variants are FP / FN
		#
		nPerfect = 0
		FPvariants = []
		alts_to_ignore = []
		for var in sorted(workflowHashed.keys()):
			if var in correctHashed:
				nPerfect += 1
				if correctHashed[var] != 3:
					correctHashed[var] = 2
				if var in correctAlts:				# ignore golden alts if one of them was found
					alreadySeen = True
					for v2 in correctAlts[var]:
						if correctHashed[v2] != 3:
							correctHashed[v2] = 3
							alreadySeen = False
					if alreadySeen:
						nPerfect -= 1
				if var in workflowAlts:
					alts_to_ignore.extend(workflowAlts[var])
			else:
				FPvariants.append(var)

		#	remove any trace of workflow variants who were not found, but whose alternate was
		for i in xrange(len(FPvariants)-1,-1,-1):
			if FPvariants[i] in alts_to_ignore:
				del FPvariants[i]
		
		# correctHashed[var] = 1: were not found
		#                    = 2: should be discluded because we were found
		#                    = 3: should be discluded because an alt was found
		notFound = [n for n in sorted(correctHashed.keys()) if correctHashed[n] == 1]

		#
		#	condense all variants who have alternate alleles and were *not* found to have perfect matches
		#	into a single variant again. These will not be included in the candidates for equivalency checking. Sorry!
		#
		notFound   = condenseAlts(notFound,correctAlts)
		FPvariants = condenseAlts(FPvariants,workflowAlts)

		#
		#
		totalVariants = nPerfect + len(notFound)
		if totalVariants == 0:
			zfP += len(FPvariants)
			ztW += workflowUnique
			print '{0:.3f} (sec)'.format(time.time()-tt)
			continue

		#
		#	let's check for equivalent variants
		#
		if FAST == False:
			delList_i = []
			delList_j = []
			regionsToCheck = []
			for i in xrange(len(FPvariants)):
				pos = FPvariants[i][0]
				regionsToCheck.append((max([pos-EV_BPRANGE-1,0]),min([pos+EV_BPRANGE,len(myDat)-1])))

			for n in regionsToCheck:
				refSection = myDat[n[0]:n[1]]

				fpWithin = []
				for i in xrange(len(FPvariants)):
					m  = FPvariants[i]
					if (m[0] > n[0] and m[0] < n[1]):
						fpWithin.append((m,i))
				fpWithin = sorted(fpWithin)
				adj = 0
				altSection = copy.deepcopy(refSection)
				for (m,i) in fpWithin:
					lr = len(m[1])
					la = len(m[2])
					dpos = m[0]-n[0]+adj
					altSection = altSection[:dpos-1] + m[2] + altSection[dpos-1+lr:]
					adj += la-lr

				nfWithin = []
				for j in xrange(len(notFound)):
					m = notFound[j]
					if (m[0] > n[0] and m[0] < n[1]):
						nfWithin.append((m,j))
				nfWithin = sorted(nfWithin)
				adj = 0
				altSection2 = copy.deepcopy(refSection)
				for (m,j) in nfWithin:
					lr = len(m[1])
					la = len(m[2])
					dpos = m[0]-n[0]+adj
					altSection2 = altSection2[:dpos-1] + m[2] + altSection2[dpos-1+lr:]
					adj += la-lr

				if altSection == altSection2:
					for (m,i) in fpWithin:
						if i not in delList_i:
							delList_i.append(i)
					for (m,j) in nfWithin:
						if j not in delList_j:
							delList_j.append(j)

			nEquiv = 0
			for i in sorted(list(set(delList_i)),reverse=True):
				del FPvariants[i]
			for j in sorted(list(set(delList_j)),reverse=True):
				del notFound[j]
				nEquiv += 1
			nPerfect += nEquiv

		#
		#	Tally up errors and whatnot
		#
		ztV += totalVariants
		ztW += workflowUnique
		znP += nPerfect
		zfP += len(FPvariants)
		znF += len(notFound)
		if FAST == False:
			znE += nEquiv
		if BEDFILE != None:
			zbM += nBelowMinRLen

		#
		#	try to identify a reason for FN variants:
		#

		venn_data = [[0,0,0] for n in notFound]		# [i] = (unmappable, low cov, low het)
		for i in xrange(len(notFound)):
			var = notFound[i]

			#	mappability?
			if MAPTRACK != None:
				if mappability_tracks[refName][var[0]]:
					mappability_vs_FN[1] += 1
					venn_data[i][0] = 1
				else:
					mappability_vs_FN[0] += 1

			#	coverage?
			if var in correctCov:
				c = correctCov[var]
				if c != None:
					if c not in coverage_vs_FN:
						coverage_vs_FN[c] = 0
					coverage_vs_FN[c] += 1
					if c < DP_THRESH:
						venn_data[i][1] = 1

			#	heterozygous genotype messing things up?
			if var in correctAF:
				a = correctAF[var]
				if a != None:
					a = AF_KEYS[quantize_AF(a)]
					if a not in alleleBal_vs_FN:
						alleleBal_vs_FN[a] = 0
					alleleBal_vs_FN[a] += 1
					if a < AF_THRESH:
						venn_data[i][2] = 1

		for i in xrange(len(notFound)):
			if venn_data[i][0]: set1.append(i+varAdj)
			if venn_data[i][1]: set2.append(i+varAdj)
			if venn_data[i][2]: set3.append(i+varAdj)
		varAdj += len(notFound)

		#
		#	if desired, write out vcf files. (This step mangles the FPvariants lists, don't use those after this!)
		#
		notFound   = sorted(notFound)
		FPvariants = sorted([n[0] for n in FPvariants])
		if VCF_OUT:
			for line in open(GOLDEN_VCF,'r'):
				if line[0] != '#':
					splt = line.split('\t')
					if splt[0] == refName:
						var  = (int(splt[1]),splt[3],splt[4])
						if var in notFound:
							vcfo2.write(line)
			for line in open(WORKFLOW_VCF,'r'):
				if line[0] != '#':
					splt = line.split('\t')
					if splt[0] == refName:
						var  = (int(splt[1]),splt[3],splt[4])
						if var in FPvariants:
							vcfo3.write(line)

		print '{0:.3f} (sec)'.format(time.time()-tt)

	#
	#	close vcf output
	#
	print ''
	if VCF_OUT:
		print OUT_PREFIX+'_FN.vcf'
		print OUT_PREFIX+'_FP.vcf'
		vcfo2.close()
		vcfo3.close()

	#
	#	plot some FN stuff
	#
	if NO_PLOT == False:
		nDetected = len(set(set1+set2+set3))
		set1 = set(set1)
		set2 = set(set2)
		set3 = set(set3)

		if len(set1): s1 = 'Unmappable'
		else: s1 = ''
		if len(set2): s2 = 'DP < '+str(DP_THRESH)
		else: s2 = ''
		if len(set3): s3 = 'AF < '+str(AF_THRESH)
		else: s3 = ''

		mpl.figure(0)
		tstr1 = 'False Negative Variants (Missed Detections)'
		tstr2 = str(nDetected)+' / '+str(znF)+' FN variants categorized'
		if MAPTRACK != None:
			v = venn3([set1, set2, set3], (s1, s2, s3))
		else:
			v = venn2([set2, set3], (s2, s3))
		mpl.figtext(0.5,0.95,tstr1,fontdict={'size':14,'weight':'bold'},horizontalalignment='center')
		mpl.figtext(0.5,0.03,tstr2,fontdict={'size':14,'weight':'bold'},horizontalalignment='center')

		ouf = OUT_PREFIX+'_FNvenn.pdf'
		print ouf
		mpl.savefig(ouf)


	#
	#	spit out results to console
	#
	print '\n**********************************\n'
	if BEDFILE != None:
		print 'ONLY CONSIDERING VARIANTS FOUND WITHIN TARGETED REGIONS\n\n'
	print 'Total Golden Variants:  ',ztV
	print 'Total Workflow Variants:',ztW,'\n'
	if ztV > 0 and ztW > 0:
		print 'Perfect Matches:',znP,'({0:.2f}%)'.format(100.*float(znP)/ztV)
		print 'FN variants:   ',znF,'({0:.2f}%)'.format(100.*float(znF)/ztV)
		print 'FP variants:   ',zfP,'({0:.2f}%)'.format(100.*float(zfP)/ztW)
	if FAST == False:
		print '\nNumber of equivalent variants denoted differently between the two vcfs:',znE
	if BEDFILE != None:
		print '\nNumber of golden variants located in targeted regions that were too small to be sampled from:',zbM
	if FAST:
		print "\nWarning! Running with '--fast' means that identical variants denoted differently between the two vcfs will not be detected! The values above may be lower than the true accuracy."
	print '\n**********************************\n'





if __name__ == '__main__':
	main()