# # -*- coding: utf-8 -*-
# """
# author: Jacob Munro, Victor Chang Cardiac Research Institute
# """
from __future__ import print_function
from __future__ import division
import re
import subprocess
import os
from subprocess import PIPE
import numpy as np
import tempfile


class SamEntry():
    # regex for processing cigar string
    cigar_clip = re.compile('^((?P<LH>[0-9]+)H)?((?P<LS>[0-9]+)S)?([0-9]+[^HS])+((?P<RS>[0-9]+)S)?((?P<RH>[0-9]+)H)?$')
    cigar_ref_chars = re.compile('[MDX=N]')
    # sam flags
    paired = np.uint16(1)
    mapped_in_proper_pair = np.uint16(2)
    read_unmapped = np.uint16(4)
    mate_unmapped = np.uint16(8)
    read_reverse = np.uint16(16)
    mate_reverse = np.uint16(32)
    first = np.uint16(64)
    second = np.uint16(128)
    secondary = np.uint16(256)
    fails_QC = np.uint16(512)
    duplicate = np.uint16(1024)
    supplementary = np.uint16(2048)

    def __init__(self, FLAG, POS, MAPQ, CIGAR, RNEXT, TLEN):
        self.flag = np.uint16(FLAG)
        self.mate_diff_molecule = '=' not in RNEXT
        self.pos = int(POS)
        self.mapQ = int(MAPQ)
        self.cigar = CIGAR
        self.tlen = int(TLEN)
        self.left, self.right = self.get_aligned_pos()

    # return the positions of the left aligned and right aligned bases
    def get_aligned_pos(self):
        clipped = re.search(SamEntry.cigar_clip, self.cigar).groupdict()
        num_aligned = 0
        num_str = ''
        for c in self.cigar:
            if c.isdigit():
                num_str += c
            else:
                if re.match(self.cigar_ref_chars, c):
                    num_aligned += int(num_str)
                num_str = ''
        left = self.pos
        if clipped['LS']:
            left += int(clipped['LS'])
        right = left + num_aligned
        return left, right

    def has_flag(self, flag):
        return self.flag & flag

    def is_rvs(self):
        return (self.flag & SamEntry.read_reverse)

    def has_unmapped_mate(self):
        return self.flag & SamEntry.mate_unmapped

    # not_primary or supplementary alignment
    def is_alt_alignment(self):
        return (self.flag & SamEntry.supplementary) or (self.flag & SamEntry.not_primary)

    def mate_same_strand(self):
        return ((self.flag & SamEntry.read_reverse) and (self.flag & SamEntry.mate_reverse)) or \
               (not(self.flag & SamEntry.read_reverse)) and (not(self.flag & SamEntry.mate_reverse))

    # assume not same strand
    def is_inverted(self):
        if not(self.flag & SamEntry.read_reverse):
            if self.tlen < 0:
                return True
        elif self.tlen > 0:
            return True
        return False

    # assume entry and mate are mapped
    def is_discordant(self):
        if (self.flag & SamEntry.read_reverse):
            if (self.flag & SamEntry.mate_reverse):
                '''  <--1 <--2  or  <--2 <--1  '''
                return True
            elif self.tlen > 0:
                '''  <--1 2-->  '''
                return True
            else:
                '''  2--> <--1  '''
                return False
        elif not (self.flag & SamEntry.read_reverse):
            if not (self.flag & SamEntry.mate_reverse):
                '''  1--> 2-->  or  2--> 1-->  '''
                return True
            elif self.tlen < 0:
                '''  <--2 1-->  '''
                return True
            else:
                '''  1--> <--2  '''
                return False
        return False

    # return the number of clipped bases
    def get_num_clipped(self):
        clipped = 0
        m = re.search(SamEntry.cigar_clip, self.cigar)
        matches = m.groupdict()
        for k in matches:
            if matches[k]:
                clipped += int(matches[k])
        return clipped


class SamStats:
    def __init__(self, aln_stats, depth_stats=None):
        # list of aln_stats for each breakpoint
        self.aln_stats = aln_stats
        # single depth stats
        self.depth_stats = depth_stats


class DepthStats:
    depth_cols = ['total', 'mapQltT', 'mapQ0']
    TOTAL = 0
    MAPQLTT = 1
    MAPQ0 = 2

    def __init__(self, bins, mapq_thresh=30, dtype=np.float):
        self.bins = bins
        self.depths = np.zeros((self.depth_bins.num, len(AlignStats.depth_cols)), dtype=dtype)
        self.mapq_thresh = mapq_thresh


    def set_depths(self, bam):
        # TODO: need to make this work for all mapQs, disable other implementation in SamStats
        bed = tempfile.NamedTemporaryFile(mode='wt', delete=False)
        for i in range(self.bins.num):
            bed.write('{}\t{}\t{}\n'.format(self.bins.chrom, self.bins.start + i*self.bins.size,
                                            self.bins.start + (i+1)*self.bins.size))
        bed_name = bed.name
        bed.close()
        depths_gt_1 = SAMtools.bedcov(self.bins.num, self.bins.size, bed_name, bam, min_Q=1)
        depths_gt_T = SAMtools.bedcov(self.bins.num, self.bins.size, bed_name, bam, min_Q=self.mapq_thresh)
        self.depths[DepthStats.TOTAL] = SAMtools.bedcov(self.bins.num, self.bins.size, bed_name, bam, min_Q=0)
        self.depths[DepthStats.MAPQ0] = self.depths[DepthStats.TOTAL] - depths_gt_1
        self.depths[DepthStats.MAPQLTT] = self.depths[DepthStats.TOTAL] - depths_gt_T - self.depths[DepthStats.MAPQ0]
        os.remove(bed_name)

    # convert depths from bp/bin count to depth/bp
    def convert_depths(self):
        # should only be done on integer count depths
        if self.depths.dtype is np.float:
            raise ValueError
        self.depths = self.depths.astype(np.float) / self.bins.size


class AlignStats:
    aln_stats_cols = ['reads', 'orphaned', 'inverted', 'samestrand', 'secondary', 'supplementary', 'clipped', 'diffmol']
    READS = 0
    ORPHANED = 1
    INVERTED = 2
    SAMESTRAND = 3
    SECONDARY = 4
    SUPPLEMENTARY = 5
    CLIPPED = 6
    DIFFMOL = 7

    def __init__(self, bkpt_bins, mapq_thresh=30, clip_thresh=1):
        # set parameters
        self.bkpt_bins = bkpt_bins
        self.mapQT = mapq_thresh
        self.clip_thresh = clip_thresh

        # initialise data structures
        self.depth_stats = []
        self.aln_stats = []
        self.fwd_inserts = []
        self.rvs_inserts = []
        for bins in self.bkpt_bins:
            self.depth_stats.append(DepthStats(bins, mapq_thresh=mapq_thresh), dtype=np.intc)
            self.aln_stats.append(np.zeros((bins.num, len(AlignStats.aln_stats_cols)), dtype=np.intc))
            self.fwd_inserts.append(np.empty(bins.num, dtype=list))
            self.rvs_inserts.append(np.empty(bins.num, dtype=list))
            for j in range(0, bins.num):
                self.fwd_inserts[-1][j] = []
                self.rvs_inserts[-1][j] = []


    def add_to_depth(self, idx, coverage, cols):
        # add start and end covered bins, partial coverage
        for j in cols:
            self.depth_stats[idx].depths[coverage[0][0]][j] += coverage[0][1]
            self.depth_stats[idx].depths[coverage[1][0]][j] += coverage[1][1]

        # add in all bins that have full coverage
        for i in range(coverage[0][0]+1, coverage[1][0]):
            for j in cols:
                self.depth_stats[idx].depths[i][j] += self..depths.bins.size

    def add_to_aln_stats(self, idx, coverage, cols):
        for i in range(coverage[0][0], coverage[1][0]+1):
            for j in cols:
                self.aln_stats[idx][i][j] += 1

    def process(self, sam_entry):
        for idx, bins in enumerate(self.bkpt_bins):

            cov = bins.get_bin_coverage(sam_entry.left, sam_entry.right)
            if cov is None:
                continue

            depth_cols = [DepthStats.TOTAL]
            if sam_entry.mapQ <= self.mapQT:
                if sam_entry.mapQ == 0:
                    depth_cols.append(DepthStats.MAPQ0)
                else:
                    depth_cols.append(DepthStats.MAPQLTT)
            self.add_to_depth(idx, cov, depth_cols)

            aln_cols = [AlignStats.READS]
            if (sam_entry.flag & SamEntry.secondary):
                aln_cols.append(AlignStats.SECONDARY)
            if (sam_entry.flag & SamEntry.supplementary):
                aln_cols.append(AlignStats.SUPPLEMENTARY)
            if sam_entry.get_num_clipped() >= self.clip_thresh:
                aln_cols.append(AlignStats.CLIPPED)
            if sam_entry.has_unmapped_mate():
                aln_cols.append(AlignStats.ORPHANED)
            else:
                if sam_entry.mate_diff_molecule:
                    aln_cols.append(AlignStats.DIFFMOL)
                elif sam_entry.mate_same_strand():
                    aln_cols.append(AlignStats.SAMESTRAND)
                elif sam_entry.is_inverted():
                    aln_cols.append(AlignStats.INVERTED)
                else:
                    # correctly oriented pair reads
                    # filter low mapQ reads as these give spurious mapping distances
                    if not sam_entry.tlen == 0 and sam_entry.mapQ > self.mapQT:
                        # pairs are mapped correctly, so add to insert sizes
                        if sam_entry.is_rvs():
                            ins_cov = bins.get_bin_coverage(sam_entry.right, sam_entry.right)
                            if ins_cov is None:
                                continue
                            self.rvs_inserts[idx][ins_cov[1][0]].append(-1 * sam_entry.tlen)
                        else:
                            ins_cov = bins.get_bin_coverage(sam_entry.left, sam_entry.left)
                            if ins_cov is None:
                                continue
                            self.fwd_inserts[idx][ins_cov[0][0]].append(sam_entry.tlen)
            self.add_to_aln_stats(idx, cov, aln_cols)

    def convert_depths(self):
        for i in range(0, self.depth_bins.num):
            for j in range(0, len(AlignStats.depth_cols)):
                self.depth_stats[i][j] /= self.depth_bins.size

    # Print the collected stats
    def print_stats(self, dir):
        depth_file = open(os.path.join(dir, 'depths.tsv'), 'w')
        aln_stats_file = open(os.path.join(dir, 'aln_stats.tsv'), 'w')
        fwd_ins_file = open(os.path.join(dir, 'fwd_ins.tsv'), 'w')
        rvs_ins_file = open(os.path.join(dir, 'rvs_ins.tsv'), 'w')

        self.convert_depths()
        # print depths
        depth_file.write('bin\t' + '\t'.join(AlignStats.depth_cols) + '\n')
        for i, row in enumerate(self.depth_stats):
            depth_file.write(str(self.depth_bins.start + i*self.depth_bins.size) + '\t')
            for j in range(0, len(row)-1):
                depth_file.write(str(row[j]) + '\t')
            depth_file.write(str(row[-1]) + '\n')
        depth_file.close()

        # print alignmet stats and insert sizes
        aln_stats_file.write('bin\t' + '\t'.join(AlignStats.aln_stats_cols) + '\n')
        for i, bin in enumerate(self.bkpt_bins):
            for j, row in enumerate(self.aln_stats[i]):
                # aln_stats
                aln_stats_file.write(str(bin.start + j*bin.size) + '\t')
                for k in range(0,len(row)-1):
                    aln_stats_file.write(str(row[k]) + '\t')
                aln_stats_file.write(str(row[k]) + '\n')
                # fwd inserts
                fwd_ins_file.write(str(bin.start + j * bin.size) + '\t')
                if self.fwd_inserts[i][j]:
                    for k in range(0, len(self.fwd_inserts[i][j])-1):
                         fwd_ins_file.write(str(self.fwd_inserts[i][j][k]) + ',')
                    fwd_ins_file.write(str(self.fwd_inserts[i][j][-1]))
                else:
                    fwd_ins_file.write('NA')
                fwd_ins_file.write('\n')
                # rvs inserts
                rvs_ins_file.write(str(bin.start + j * bin.size) + '\t')
                if self.rvs_inserts[i][j]:
                    for k in range(0, len(self.rvs_inserts[i][j])-1):
                        rvs_ins_file.write(str(self.rvs_inserts[i][j][k]) + ',')
                    rvs_ins_file.write(str(self.rvs_inserts[i][j][-1]))
                else:
                    rvs_ins_file.write('NA')
                rvs_ins_file.write('\n')
        fwd_ins_file.close()
        rvs_ins_file.close()
        aln_stats_file.close()

    # returns a list of sam_stats corresponding to the list of bams given for this position
    @staticmethod
    def get_sam_stats(bams, depth_bins=None, bkpt_bins=None):
        if depth_bins or bkpt_bins:
            if not bkpt_bins:
                bkpt_bins = [depth_bins]
            sam_stats = []
            for bam in bams:
                sam_stats.append(SamStats(AlignStats(bkpt_bins), depth_stats=DepthStats(depth_bins)))
                sam_stats[-1].depth_stats.set_depths(bam)

                for bkpt in bkpt_bins:
                    p = SAMtools.view(bam, bkpt.region)
                    line = p.stdout.readline()
                    while line:
                        try:
                            FLAG, RNAME, POS, MAPQ, CIGAR, RNEXT, PNEXT, TLEN = line.split()[1:9]
                        except ValueError:
                            line = p.stdout.readline()
                            continue
                        else:
                            sam_stats[-1].process(SamEntry(FLAG, POS, MAPQ, CIGAR, RNEXT, TLEN))
                            line = p.stdout.readline()
            return sam_stats
        else:
            raise ValueError("Must provide at least one of depth_bins, bkpt_A_bins or bkpt_B_bins")


class SAMtools:
    @staticmethod
    def check_installation():
        cmd = ['samtools', '--version-only']
        try:
            subprocess.check_output(cmd, universal_newlines=True)
        except OSError:
            print('Error: could not run samtools. Are you sure it is installed?')
            exit(1)

    @staticmethod
    def view(sam, region, include_flag=None, exclude_flag=
            (SamEntry.duplicate + SamEntry.fails_QC + SamEntry.read_unmapped), samtools='samtools'):
        cmd = [samtools, 'view']
        if include_flag:
            cmd.append('-f')
            cmd.append(str(include_flag))
        if exclude_flag:
            cmd.append('-F')
            cmd.append(str(exclude_flag))
        cmd.append(sam)
        cmd.append(region)
        print(' '.join(cmd) + '\n')
        p = subprocess.Popen(cmd, bufsize=1024, stdout=PIPE, universal_newlines=True)
        if p.poll():
            print("Error code %d from command:\n%s\n" % (' '.join(cmd) + '\n'))
            exit(1)
        return p

    @staticmethod
    def faidx(fasta, region, samtools='samtools'):
        cmd = [samtools, 'faidx', fasta, region]
        print(' '.join(cmd) + '\n')
        p = subprocess.Popen(cmd, bufsize=1024, stdout=PIPE, universal_newlines=True)
        if p.poll():
            print("Error code %d from command:\n%s\n" % (' '.join(cmd) + '\n'))
            exit(1)
        return p

    @staticmethod
    def bedcov(num_bins, bin_size, bed, bam, min_Q=30):
        cmd = ['samtools', 'bedcov', '-Q', str(min_Q), bed, bam]
        p = subprocess.Popen(cmd, bufsize=-1, stdout=subprocess.PIPE, universal_newlines=True)
        data = np.zeros((num_bins,))
        bin = 0
        bin_start = None
        line = p.stdout.readline()
        values = []

        while line:
            try:
                chrom, start, end, cov = line.split()
            except ValueError:
                print('unexpected number of fields in samtools bedcov output\n')
                break
            line = p.stdout.readline()
            if bin_start is None:
                bin_start = int(start)
                values = [int(cov) / (int(end) - int(start) + 1)]
            else:
                if int(start) - bin_start < bin_size:
                    values.append(int(cov) / (int(end) - int(start) + 1))
                else:
                    data[bin] = np.mean(values)
                    bin_start = int(start)
                    values = [int(cov) / (int(end) - int(start) + 1)]
                    bin += 1
                    if bin >= num_bins:
                        print('Error: exceeded allocation for this region')
                        break
        data[bin] = np.mean(values)
        if bin + 1 != num_bins:
            print('Error: allocation for this region not filled')
            print('{} of {} bins'.format(bin + 1, num_bins))
        return data

    @staticmethod
    def get_GC(fasta, region):
        GC = 0
        AT = 0
        p = SAMtools.faidx(fasta, region)
        line = p.stdout.readline()
        while line:
            if line[0] != '>':
                for base in line:
                    if base.upper() in ('G', 'C'):
                        GC += 1
                    elif base.upper() in ('A', 'T'):
                        AT += 1
            line = p.stdout.readline()
        return GC / (AT + GC)
