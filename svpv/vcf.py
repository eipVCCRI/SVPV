# # -*- coding: utf-8 -*-
# """
# author: Jacob Munro, Victor Chang Cardiac Research Institute
# """
from __future__ import print_function
from __future__ import division
import subprocess
from subprocess import PIPE
import copy, re


class VCFManager:
    vcf_count = 1

    def __init__(self, vcf_file, name='VCF ' + str(vcf_count), db_mode=False):
        VCFManager.vcf_count += 1
        self.name = name
        self.samples = BCFtools.get_samples(vcf_file)
        # count of svs in vcf
        self.count = 0
        # dict by chrom of dict by pos of lists of SVs
        self.SVs = {}
        # dict of chr, sorted lists of positions
        self.positions = {}
        self.set_svs(vcf_file, db_mode)


    # read in all SV sites
    def set_svs(self, vcf, db_mode):
        p = BCFtools.get_SV_sites(vcf, db_mode)
        line = p.stdout.readline()
        bnds = BNDs()
        while line:
            sv = SV.parse_sv(line, db_mode)
            if sv is not None:
                if isinstance(sv, BND_SV):
                    bnds.add_BND(sv)
                else:
                    self.count += 1
                    if sv.chrom in self.SVs:
                        if sv.pos in self.SVs[sv.chrom]:
                            self.SVs[sv.chrom][sv.pos].append(sv)
                        else:
                            self.SVs[sv.chrom][sv.pos] = [sv]
                    else:
                        self.SVs[sv.chrom] = {}
                        self.SVs[sv.chrom][sv.pos] = [sv]
                    # delly
                    if sv.svtype == 'TRA':
                        if sv.chr2 in self.SVs:
                            if sv.pos in self.SVs[sv.chr2]:
                                self.SVs[sv.chr2][sv.end].append(sv)
                            else:
                                self.SVs[sv.chr2][sv.end] = [sv]
                        else:
                            self.SVs[sv.chr2] = {}
                            self.SVs[sv.chr2][sv.end] = [sv]
            line = p.stdout.readline()
        # add processed breakends into SVs
        for bnd_e in bnds.get_events():
            for bnd in bnd_e.bnds:
                if bnd.chrom in self.SVs:
                    if bnd.pos in self.SVs[bnd.chrom ]:
                        self.SVs[bnd.chrom ][bnd.pos].append(bnd)
                    else:
                        self.SVs[bnd.chrom ][bnd.pos] = [bnd]
                else:
                    self.SVs[bnd.chrom ] = {}
                    self.SVs[bnd.chrom ][bnd.pos] = [bnd]
        # keep track of where SVs are for faster query
        for chr in self.SVs:
            self.positions[chr] = sorted(list(self.SVs[chr].keys()))

    # for all svs, remove those that have not been called in the list of samples
    def remove_absent_svs(self, samples):
        idxs = []
        for s in samples:
            if s in self.samples:
                idxs.append(self.samples.index(s))

        for chrom in self.SVs:
            delete = []
            for i, sv in enumerate(self.SVs[chrom]):
                present = False
                for idx in idxs:
                    if '1' in sv.GTs[idx]:
                        present = True
                        break
                if not present:
                    delete.append(i)
            for i in sorted(delete, reverse=True):
                del self.SVs[chrom][i]
                self.count -= 1

    # return all SV calls that overlap with given range
    def get_svs_in_range(self, chrom, start, end, sample=None):
        ret = []
        if chrom in self.positions:
            for pos in self.positions[chrom]:
                if pos > end:
                    break
                for sv in self.SVs[chrom][pos]:
                    if sv.pos > end:
                        continue
                    if sv.end < start:
                        continue
                    else:
                        ret.append(sv)

        if sample is not None:
            delete = []
            for i, sv in enumerate(ret):
                if '1' not in sv.GTs[self.samples.index(sample)]:
                    delete.append(i)
            for i in sorted(delete, reverse=True):
                del ret[i]
        return ret

    def get_sample_index(self, sample):
        if sample in self.samples:
            return self.samples.index(sample)
    
    def filter_svs(self, filter_par, as_list=False):
        matching = {}
        if filter_par.chrom:
            if filter_par.chrom in self.SVs:
                matching[filter_par.chrom] = copy.copy(self.SVs[filter_par.chrom])
            else:
                # no SVs in chrom, return empty dict
                return matching
        else:
            matching = {}
            for chrom in self.SVs:
                matching[chrom] = copy.copy(self.SVs[chrom])

        # filter by svtype
        if filter_par.svtype:
            for chrom in matching:
                delete = []
                for i in range(0, len(matching[chrom])):
                    if matching[chrom][i].svtype != filter_par.svtype:
                        delete.append(i)
                delete.sort(reverse=True)
                for i in delete:
                    del matching[chrom][i]

        if filter_par.sample_GTs:
            # go through all SVs currently in matching and remove those without the correct genotype
            for chrom in matching:
                delete = []
                for i in range(0, len(matching[chrom])):
                    for sample in filter_par.sample_GTs:
                        if matching[chrom][i].GTs[self.get_sample_index(sample)] not in filter_par.sample_GTs[sample] \
                                and '*' not in filter_par.sample_GTs[sample]:
                            delete.append(i)
                            break
                delete.sort(reverse=True)
                for i in delete:
                    del matching[chrom][i]
        # filter by maf
        if filter_par.AF_thresh:
            for chrom in matching:
                delete = []
                for i, sv in enumerate(matching[chrom]):
                    if filter_par.AF_thresh_is_LT:
                        if not sv.AF < filter_par.AF_thresh:
                            delete.append(i)
                    else:
                        if not sv.AF > filter_par.AF_thresh:
                            delete.append(i)
                delete.sort(reverse=True)
                for i in delete:
                    del matching[chrom][i]

        # filter by ref_genes/intersection with specific gene
        if filter_par.RG_intersection or filter_par.gene_list_intersection or filter_par.exonic:
            for chrom in matching:
                delete = []
                for i, sv in enumerate(matching[chrom]):
                    intersecting = filter_par.ref_genes.get_entries_in_range(chrom, sv.pos, sv.end)
                    if not intersecting:
                        delete.append(i)
                    elif filter_par.gene_list_intersection:
                        in_gene_list = False
                        for gene in intersecting:
                            if gene.name2.upper() in filter_par.gene_list:
                                if filter_par.exonic and not gene.intersects_exon(sv.pos, sv.end):
                                    continue
                                in_gene_list = True
                                break
                        if not in_gene_list:
                            delete.append(i)
                    elif filter_par.exonic:
                        exonic = False
                        for gene in intersecting:
                            if gene.intersects_exon(sv.pos, sv.end):
                                exonic = True
                                break
                        if not exonic:
                            delete.append(i)

                delete.sort(reverse=True)
                for i in delete:
                    del matching[chrom][i]

        # filter by SV length
        if (not filter_par.min_len == None) or (not filter_par.max_len == None):
            for chrom in matching:
                delete = []
                for i, sv in enumerate(matching[chrom]):
                    if not filter_par.min_len == None:
                        if (sv.end - sv.pos + 1) < filter_par.min_len:
                            delete.append(i)
                            continue
                    if not filter_par.max_len == None:
                        if (sv.end - sv.pos + 1) > filter_par.max_len:
                            delete.append(i)
                            continue
                delete.sort(reverse=True)
                for i in delete:
                    del matching[chrom][i]

        # return the filtered set of SVs
        if as_list:
            listed = []
            for chrom in sorted(matching.keys()):
                listed.extend(matching[chrom])
            return listed
        return matching


# basic vcf SV class
class SV:
    valid_SVs = ['DEL', 'DUP', 'CNV', 'INV', 'TRA', 'INS', 'BND']

    def __init__(self, chrom, pos, end, svtype, svlen, inslen, chr2, gts=None, af=float(0)):
        self.chrom = chrom
        self.pos = int(pos)
        try:
            self.end = int(end)
        except ValueError:
            self.end = self.pos
            pass

        # delly sv field
        if inslen != '.':
            self.inslen = int(inslen)
        else:
            self.inslen = None
        # delly sv field
        if chr2 != '.':
            self.chr2 = chr2
            self.chr2_pos = self.end
            self.end = self.pos
        else:
            self.chr2 = None
            self.chr2_pos = None

        if svlen != '.':
            self.len = abs(int(svlen))
        elif self.inslen:
            self.len = int(inslen)
        else:
            if self.pos and self.end:
                self.len = self.end - self.pos + 1
            else:
                self.len = None

        self.svtype = svtype

        self.GTs = gts
        if gts:
            self.AF = self.get_AF()
        else:
            self.AF = float(af)

    @staticmethod
    def parse_sv(line, db_mode):
        try:
            if db_mode:
                chrom, pos, id, alt, end, svtype, svlen, eventid, pairid, mateid, inslen, chr2, af = line.split()[0:13]
                if svtype not in SV.valid_SVs:
                    svtype = re.sub('[<>]', '', alt)
                if svtype in SV.valid_SVs:
                    if svtype == 'BND':
                        return(BND_SV(chrom, pos, end, svtype, svlen, inslen, chr2, alt, id, mateid, pairid, eventid,
                                      af=af))
                    else:
                        return SV(chrom, pos, end, svtype, svlen, inslen, chr2, af=af)
            else:
                chrom, pos, id, alt, end, svtype, svlen, eventid, pairid, mateid, inslen, chr2 = line.split()[0:12]
                gts = line.split()[12:]
                if svtype not in SV.valid_SVs:
                    svtype = re.sub('[<>]', '', alt)
                if svtype in SV.valid_SVs:
                    if svtype == 'BND':
                        return BND_SV(chrom, pos, end, svtype, svlen, inslen, chr2, alt, id, mateid, pairid, eventid,
                                      gts=gts)
                    else:
                        return SV(chrom, pos, end, svtype, svlen, inslen, chr2, gts=gts)
        except ValueError:
            pass
        print('SV parsing failed for line:\n{}'.format(line))
        return None

    def get_AF(self):
        if len(self.GTs):
            n = 0
            for gt in self.GTs:
                if '1' in gt:
                    if '1/1' in gt:
                        n += 2
                    else:
                        n += 1
            return n / (2 * len(self.GTs))

    # helper method for print_SVs
    def to_string(self, sample_index=None):
        if sample_index is not None:
            return '\t'.join([self.chrom, str(self.pos), str(self.end), self.svtype, self.GTs[sample_index]]) + '\n'
        else:
            return '\t'.join([self.chrom, str(self.pos), str(self.end), self.svtype, str(self.AF)]) + '\n'

    def string_tuple(self):
        if self.svtype in ('TRA', 'INS'):
            l = 'NA'
        else:
            l = str(self.end - self.pos + 1)
        return (self.svtype, self.chrom, str(self.pos), l, ('{0:.2f}'.format(self.AF)))

    # print SVs, either with genotype per sample, or MAF for whole BATCH annotation
    @staticmethod
    def print_SVs(SVs, out, vcf_name, sample_index=None):
        for sv in SVs:
            out.write(vcf_name + '\t' + sv.to_string(sample_index=sample_index))

    @staticmethod
    def print_SVs_header(out, sample_index=None):
        if not sample_index == None:
            out.write('\t'.join(['vcf', 'chrom', 'start', 'end', 'svtype', 'gt']) + '\n')
        else:
            out.write('\t'.join(['vcf', 'chrom', 'start', 'end', 'svtype', 'MAF']) + '\n')


# extending the SV class to include information relevant to breakends only
class BND_SV(SV):
    right_fwd = re.compile('^.+\[(?P<chr>.+):(?P<pos>.+)\[$')
    right_rvs = re.compile('^.+\](?P<chr>.+):(?P<pos>.+)\]$')
    left_fwd = re.compile('^\](?P<chr>.+):(?P<pos>.+)\].+$')
    left_rvs = re.compile('^\[(?P<chr>.+):(?P<pos>.+)\[.+$')

    def __init__(self, chrom, start, end, svtype, svlen, inslen, chr2, ALT, ID, MATEID, PAIRID, EVENTID,
                 gts=None, af=float(0)):
        SV.__init__(self, chrom, start, end, svtype, svlen, inslen, chr2, gts=gts, af=af)
        if ID == '.':
            raise ValueError
        self.id = ID
        if MATEID != '.':
            self.mate_id = MATEID
        else:
            self.mate_id = None
        if PAIRID != '.':
            self.pair_id = PAIRID
        else:
            self.pair_id = None
        if EVENTID != '.':
            self.event_id = EVENTID
        else:
            self.event_id = None

        if re.match(BND_SV.right_fwd, ALT):
            m = re.match(BND_SV.right_fwd, ALT)
            self.type = 'right_fwd'
        elif re.match(BND_SV.right_rvs, ALT):
            m = re.match(BND_SV.right_rvs, ALT)
            self.type = 'right_rvs'
        elif re.match(BND_SV.left_fwd, ALT):
            m = re.match(BND_SV.left_fwd, ALT)
            self.type = 'left_fwd'
        elif re.match(BND_SV.left_rvs, ALT):
            m = re.match(BND_SV.left_rvs, ALT)
            self.type = 'left_rvs'
        else:
            return None

        self.mate_chr = m.group('chr')
        self.mate_pos = int(m.group('pos'))

        self.BND_Event = None
        self.MATE = None

# class to manage the set of breakends in a VCF
class BNDs:
    def __init__(self):
        # store bnds with EVENTID together as a list of ids
        self.events = {}
        # store remainder (no EVENTID) as set
        self.non_events = set()
        # store each bnd, and their relationships
        self.BNDs = {}
        self.mates = {}
        self.pairs = {}

    def add_BND(self, bnd_sv):
        self.BNDs[bnd_sv.id] = bnd_sv
        # update the list in events
        if bnd_sv.event_id is not None:
            if bnd_sv.event_id in self.events:
                if bnd_sv not in self.events[bnd_sv.event_id]:
                    self.events[bnd_sv.event_id].append(bnd_sv.id)
            else:
                self.events[bnd_sv.event_id] = [bnd_sv.id]
        else:
            self.non_events.add(bnd_sv.id)
        # update the pointers in mates and pairs
        if bnd_sv.mate_id is not None:
            if bnd_sv.mate_id in self.BNDs:
                self.mates[bnd_sv.id] = bnd_sv.mate_id
                self.mates[bnd_sv.mate_id] = bnd_sv.id
                # maintain a reference for future use
                bnd_sv.MATE = self.BNDs[bnd_sv.mate_id]
                self.BNDs[bnd_sv.mate_id].MATE = bnd_sv
        if bnd_sv.pair_id is not None:
            if bnd_sv.pair_id in self.BNDs:
                self.mates[bnd_sv.id] = bnd_sv.pair_id
                self.mates[bnd_sv.pair_id] = bnd_sv.id


    # get a grouping of bnds that don't have an assigned event id
    def pop_non_event(self):
        ne = self.non_events.pop()
        bnds = [ne]
        if ne in self.mates:
            bnds.append(self.mates[ne])
            self.non_events.discard(bnds[-1])
            if bnds[-1] in self.pairs:
                if self.pairs[bnds[-1]] not in bnds:
                    bnds.append(self.pairs[bnds[-1]])
                    self.non_events.discard(bnds[-1])
        if ne in self.pairs:
            if self.pairs[ne] not in bnds:
                bnds.append(self.pairs[ne])
            if bnds[-1] in self.mates:
                if self.mates[bnds[-1]] not in bnds:
                    bnds.append(self.mates[bnds[-1]])
                    self.non_events.discard(bnds[-1])
        return bnds

    # process and return the list of bnd events to include in SVPV
    def get_events(self):
        bnd_events = []
        for e in self.events:
            try:
                bnd_events.append(BND_Event([self.BNDs[k] for k in self.events[e]]))
            except ValueError:
                pass
        while len(self.non_events) > 0:
            try:
                bnds = self.pop_non_event()
                if bnds:
                    bnd_events.append(BND_Event([self.BNDs[k] for k in bnds]))
            except ValueError:
                pass

        return bnd_events

# class to hold a BND event
# currently only two loci (distinct genomic positions) are supported
class BND_Event():
    def __init__(self, bnds, delta=20, max_loci=2):
        self.bnds = bnds
        self.loci = []
        for bnd in bnds:
            bnd.BND_Event = self
            if self.loci:
                is_proximal = False
                for chr, pos in self.loci:
                    if bnd.chrom == chr:
                        if pos - delta <= bnd.pos <= pos + delta:
                            is_proximal = True
                if not is_proximal:
                    if len(self.loci) < max_loci:
                        self.loci.append((bnd.chrom, bnd.pos))
                    else:
                        # skip this BND_Event as is too complex to display
                        raise ValueError
            else:
                self.loci.append((bnd.chrom, bnd.pos))

class BCFtools:
    @staticmethod
    def check_installation():
        cmd = ['bcftools', '--version-only']
        try:
            subprocess.check_output(cmd, universal_newlines=True)
        except OSError:
            print('Error: could not run bcftools. Are you sure it is installed?')
            exit(1)

    # return a pipe to the set of sv sites
    @staticmethod
    def get_SV_sites(vcf, db_mode=False):
        cmd = ["bcftools", "query", "-u", "-f"]
        if db_mode:
            cmd.append("%CHROM\\t%POS\\t%ID\\t%ALT{0}\\t%INFO/END\\t%INFO/SVTYPE\\t%INFO/SVLEN\\t%INFO/EVENTID"
                       "\\t%INFO/PAIRID\\t%INFO/MATEID\\t%INFO/INSLEN\\t%INFO/CHR2\\t%INFO/AF\\n")
        else:
            cmd.append("%CHROM\\t%POS\\t%ID\\t%ALT{0}\\t%INFO/END\\t%INFO/SVTYPE\\t%INFO/SVLEN\\t%INFO/EVENTID"
                       "\\t%INFO/PAIRID\\t%INFO/MATEID\\t%INFO/INSLEN\\t%INFO/CHR2[\\t%GT]\\n")
        cmd.append(vcf)
        print(' '.join(cmd) + '\n')
        p = subprocess.Popen(cmd, bufsize=1024, stdout=PIPE, universal_newlines=True)
        if p.poll():
            print("Error code %d from command:\n%s\n" % (' '.join(cmd) + '\n'))
            exit(1)
        return p

    # return a list of samples
    @staticmethod
    def get_samples(vcf):
        cmd = ["bcftools", "query", "-l", vcf]
        print(' '.join(cmd) + '\n')
        return subprocess.check_output(cmd, universal_newlines=True).split()
