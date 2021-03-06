import tempdir
import logging
import os.path
import shutil
import extern
import itertools
import tempfile
import json
import re
from Bio import SeqIO
from io import StringIO

from .singlem import HmmDatabase, TaxonomyFile, OrfMUtils
from .otu_table import OtuTable
from .known_otu_table import KnownOtuTable
from .metagenome_otu_finder import MetagenomeOtuFinder
from .sequence_classes import SeqReader, AlignedProteinSequence
from .diamond_parser import DiamondResultParser
from .graftm_result import GraftMResult
from . import sequence_extractor as singlem_sequence_extractor
from .placement_parser import PlacementParser
from .taxonomy_bihash import TaxonomyBihash

from graftm.sequence_extractor import SequenceExtractor
from graftm.greengenes_taxonomy import GreenGenesTaxonomy
from graftm.sequence_search_results import HMMSearchResult, SequenceSearchResult
from graftm.sequence_io import SequenceIO

PPLACER_ASSIGNMENT_METHOD = 'pplacer'
DIAMOND_ASSIGNMENT_METHOD = 'diamond'
DIAMOND_EXAMPLE_BEST_HIT_ASSIGNMENT_METHOD = 'diamond_example'
NO_ASSIGNMENT_METHOD = 'no_assign_taxonomy'

class SearchPipe:
    DEFAULT_MIN_ORF_LENGTH = 96
    DEFAULT_FILTER_MINIMUM_PROTEIN = 28
    DEFAULT_FILTER_MINIMUM_NUCLEOTIDE = 95

    def run(self, **kwargs):
        output_otu_table = kwargs.pop('otu_table', None)
        archive_otu_table = kwargs.pop('archive_otu_table', None)
        output_extras = kwargs.pop('output_extras')
        singlem_packages = kwargs['singlem_packages']

        otu_table_object = self.run_to_otu_table(**kwargs)
        if otu_table_object is not None:
            self.write_otu_tables(
                otu_table_object,
                output_otu_table,
                archive_otu_table,
                output_extras,
                singlem_packages)

    def write_otu_tables(self,
            otu_table_object,
            output_otu_table,
            archive_otu_table,
            output_extras,
            singlem_packages):
        regular_output_fields = str.split('gene sample sequence num_hits coverage taxonomy')
        otu_table_object.fields = regular_output_fields + \
            str.split('read_names nucleotides_aligned taxonomy_by_known?')
        if output_otu_table:
            with open(output_otu_table, 'w') as f:
                if output_extras:
                    otu_table_object.write_to(f, otu_table_object.fields)
                else:
                    otu_table_object.write_to(f, regular_output_fields)
        if archive_otu_table:
            with open(archive_otu_table, 'w') as f:
                otu_table_object.archive(HmmDatabase(singlem_packages)).write_to(f)


    def run_to_otu_table(self, **kwargs):
        '''Run the pipe, '''
        forward_read_files = kwargs.pop('sequences')
        reverse_read_files = kwargs.pop('reverse_read_files', None)
        num_threads = kwargs.pop('threads')
        known_otu_tables = kwargs.pop('known_otu_tables')
        singlem_assignment_method = kwargs.pop('assignment_method')
        output_jplace = kwargs.pop('output_jplace')
        evalue = kwargs.pop('evalue')
        min_orf_length = kwargs.pop('min_orf_length')
        restrict_read_length = kwargs.pop('restrict_read_length')
        filter_minimum_protein = kwargs.pop('filter_minimum_protein')
        filter_minimum_nucleotide = kwargs.pop('filter_minimum_nucleotide')
        include_inserts = kwargs.pop('include_inserts')
        singlem_packages = kwargs.pop('singlem_packages')
        assign_taxonomy = kwargs.pop('assign_taxonomy')
        known_sequence_taxonomy = kwargs.pop('known_sequence_taxonomy')
        diamond_prefilter = kwargs.pop('diamond_prefilter')

        working_directory = kwargs.pop('working_directory')
        working_directory_tmpdir = kwargs.pop('working_directory_tmpdir')
        force = kwargs.pop('force')
        if len(kwargs) > 0:
            raise Exception("Unexpected arguments detected: %s" % kwargs)

        self._num_threads = num_threads
        self._evalue = evalue
        self._min_orf_length = min_orf_length
        self._restrict_read_length = restrict_read_length
        self._filter_minimum_protein = filter_minimum_protein
        self._filter_minimum_nucleotide = filter_minimum_nucleotide

        hmms = HmmDatabase(singlem_packages)
        if singlem_assignment_method == DIAMOND_EXAMPLE_BEST_HIT_ASSIGNMENT_METHOD:
            graftm_assignment_method = DIAMOND_ASSIGNMENT_METHOD
        else:
            graftm_assignment_method = singlem_assignment_method

        analysing_pairs = reverse_read_files is not None
        if analysing_pairs:
            if len(forward_read_files) != len(reverse_read_files):
                raise Exception("When analysing paired input data, the number of forward read files must be the same as the number of reverse read files")
            for pkg in hmms:
                if not pkg.is_protein_package():
                    raise Exception(
                        "Paired read inputs can only be used with protein SingleM packages, but support may be added in the future.")

        if logging.getLevelName(logging.getLogger().level) == 'DEBUG':
            self._graftm_verbosity = '5'
        else:
            self._graftm_verbosity = '2'

        if not assign_taxonomy:
            singlem_assignment_method = NO_ASSIGNMENT_METHOD

        using_temporary_working_directory = working_directory is None
        if using_temporary_working_directory:
            if working_directory_tmpdir is False:
                shared_mem_directory = '/dev/shm'
                if os.path.exists(shared_mem_directory):
                    logging.debug("Using shared memory as a base directory")
                    tmp = tempdir.TempDir(basedir=shared_mem_directory)
                    tempfiles_path = os.path.join(tmp.name, 'tempfiles')
                    os.mkdir(tempfiles_path)
                    os.environ['TEMP'] = tempfiles_path
                else:
                    logging.debug("Shared memory directory not detected, using default temporary directory instead")
                    tmp = tempdir.TempDir()
                working_directory = tmp.name
            else:
                logging.debug("Using conventional temporary directory as working directory")
                tmp = tempdir.TempDir()
                tempfiles_path = os.path.join(tmp.name, 'tempfiles')
                os.mkdir(tempfiles_path)
                os.environ['TEMP'] = tempfiles_path
                working_directory = tmp.name
        else:
            working_directory = working_directory
            if os.path.exists(working_directory):
                if force:
                    logging.info("Overwriting directory %s" % working_directory)
                    shutil.rmtree(working_directory)
                    os.mkdir(working_directory)
                else:
                    raise Exception("Working directory '%s' already exists, not continuing" % working_directory)
            else:
                os.mkdir(working_directory)
        logging.debug("Using working directory %s" % working_directory)
        self._working_directory = working_directory
        extracted_reads = None
        def return_cleanly():
            if using_temporary_working_directory: tmp.dissolve()
            logging.info("Finished")
        # Set a tempfile directory in the working directory so that temporary
        # files can be generated (with delete=False), and then immediately
        # closed so that the file exists but the stream is not open, to avoid
        # the "Too many open files" error.
        tempfile_directory = os.path.join(self._working_directory, 'tmp')
        os.mkdir(tempfile_directory)
        tempfile.tempdir = tempfile_directory

        #### Search
        self._singlem_package_database = hmms
        logging.info("Using as input %i different sequence files e.g. %s" % (
            len(forward_read_files), forward_read_files[0]))
        
        if diamond_prefilter:
            logging.info("Filtering sequence files through DIAMOND blastx")
            for pkg in hmms:
                if not pkg.is_protein_package():
                    raise Exception(
                        "DIAMOND prefilter cannot be used with nucleotide SingleM packages")
            
            dmnd = hmms.get_dmnd()
            forward_read_files = self._prefilter(dmnd, forward_read_files)
            if reverse_read_files != None:
                reverse_read_files = self._prefilter(dmnd, reverse_read_files, True)
            logging.info("Finished DIAMOND prefilter phase")
                
        search_result = self._search(hmms, forward_read_files, reverse_read_files)
        sample_names = search_result.samples_with_hits()
        if len(sample_names) == 0:
            logging.info("No reads identified in any samples, stopping")
            return_cleanly()
            return None
        logging.debug("Recovered %i samples with at least one hit e.g. '%s'"
                     % (len(sample_names), sample_names[0]))

        #### Alignment
        align_result = self._align(search_result)

        ### Extract reads that have already known taxonomy
        if known_otu_tables:
            logging.info("Parsing known taxonomy OTU tables")
            known_taxes = KnownOtuTable()
            known_taxes.parse_otu_tables(known_otu_tables)
            logging.debug("Read in %i sequences with known taxonomy" % len(known_taxes))
        else:
            known_taxes = []
        if known_sequence_taxonomy:
            logging.debug("Parsing sequence-wise taxonomy..")
            tax1 = GreenGenesTaxonomy.read(open(known_sequence_taxonomy)).taxonomy
            known_sequence_tax = {}
            for seq_id, tax in tax1.items():
                known_sequence_tax[seq_id] = '; '.join(tax)
            logging.info("Read in %i taxonomies from the GreenGenes format taxonomy file" % len(known_sequence_tax))

        ### Extract other reads which do not have known taxonomy
        extracted_reads = self._extract_relevant_reads(
            align_result, include_inserts, known_taxes)
        logging.info("Finished extracting aligned sequences")

        #### Taxonomic assignment
        if assign_taxonomy:
            logging.info("Running taxonomic assignment with GraftM..")
            assignment_result = self._assign_taxonomy(
                extracted_reads, graftm_assignment_method)

        #### Process taxonomically assigned reads
        otu_table_object = OtuTable()
        package_to_taxonomy_bihash = {}
        for readset in extracted_reads:
            self._process_taxonomically_assigned_reads(
                # inputs
                readset,
                analysing_pairs,
                known_taxes,
                known_sequence_taxonomy,
                assign_taxonomy,
                singlem_assignment_method,
                assignment_result if assign_taxonomy else None,
                output_jplace,
                known_sequence_tax if known_sequence_taxonomy else None,
                # outputs
                otu_table_object,
                package_to_taxonomy_bihash)

        return_cleanly()
        return otu_table_object

    def _get_windowed_sequences(self, protein_sequences, nucleotide_sequence_file,
                                singlem_package, include_inserts):
        if not os.path.exists(nucleotide_sequence_file) or \
            os.stat(nucleotide_sequence_file).st_size == 0: return []
        nucleotide_sequences = SeqReader().read_nucleotide_sequences(nucleotide_sequence_file)
        protein_alignment = self._align_proteins_to_hmm(
            protein_sequences,
            singlem_package.graftm_package().alignment_hmm_path())
        return MetagenomeOtuFinder().find_windowed_sequences(
            protein_alignment,
            nucleotide_sequences,
            singlem_package.window_size(),
            include_inserts,
            singlem_package.is_protein_package(),
            best_position=singlem_package.singlem_position())

    def _extract_relevant_reads(self, alignment_result, include_inserts, known_taxonomy):
        '''Given a SingleMPipeAlignSearchResult, extract reads that will be used as
        part of the singlem choppage process.

        Returns
        -------
        ExtractedReads object
        '''

        def extract_reads(
                singlem_package,
                prealigned_protein_sequences,
                nucleotide_sequence_fasta_file,
                include_inserts,
                known_taxonomy,
                read_direction):

            aligned_seqs = self._get_windowed_sequences(
                prealigned_protein_sequences,
                nucleotide_sequence_fasta_file,
                singlem_package,
                include_inserts)

            known_sequences = []
            unknown_sequences = []
            for s in aligned_seqs:
                if s.aligned_sequence in known_taxonomy:
                    known_sequences.append(s)
                else:
                    unknown_sequences.append(s)
            logging.debug("For sample {} ({}), spkg {}, found {} known and {} unknown OTU sequences".format(
                sample_name,
                read_direction,
                singlem_package.base_directory(),
                len(known_sequences),
                len(unknown_sequences)))

            if len(unknown_sequences) > 0:
                extractor = singlem_sequence_extractor.SequenceExtractor()
                logging.debug("Extracting reads for {} from {} ..".format(
                    singlem_package.base_directory(), nucleotide_sequence_fasta_file
                ))
                seqs = extractor.extract_and_read(
                    [s.name for s in unknown_sequences],
                    nucleotide_sequence_fasta_file)
            else:
                seqs = []
            readset = ExtractedReadSet(
                sample_name, singlem_package,
                seqs, known_sequences, unknown_sequences)
            return readset

        extracted_reads = ExtractedReads(alignment_result.analysing_pairs)

        for sample_name in alignment_result.sample_names():
            for singlem_package in self._singlem_package_database:
                for prealigned_file in alignment_result.prealigned_sequence_files(
                        sample_name, singlem_package):
                    if singlem_package.is_protein_package():
                        nucleotide_sequence_fasta_file = \
                            alignment_result.nucleotide_sequence_file(
                                sample_name, singlem_package)
                    else:
                        nucleotide_sequence_fasta_file = prealigned_file

                    if alignment_result.analysing_pairs:
                        logging.debug("Extracting forward reads")
                        if os.path.exists(prealigned_file[0]):
                            prots = SeqReader().readfq(open(prealigned_file[0]))
                        else:
                            prots = []
                        readset1 = extract_reads(
                            singlem_package,
                            prots,
                            nucleotide_sequence_fasta_file[0],
                            include_inserts,
                            known_taxonomy,
                            'forward')
                        logging.debug("Extracting reverse reads")
                        if os.path.exists(prealigned_file[1]):
                            prots = SeqReader().readfq(open(prealigned_file[1]))
                        else:
                            prots = []
                        readset2 = extract_reads(
                            singlem_package,
                            prots,
                            nucleotide_sequence_fasta_file[1],
                            include_inserts,
                            known_taxonomy,
                            'reverse')
                        extracted_reads.add([readset1, readset2])
                    else:
                        if os.path.exists(prealigned_file):
                            prots = SeqReader().readfq(open(prealigned_file))
                        else:
                            prots = []
                        readset = extract_reads(
                            singlem_package,
                            prots,
                            nucleotide_sequence_fasta_file,
                            include_inserts,
                            known_taxonomy,
                            'forward')
                        extracted_reads.add(readset)

        return extracted_reads

    def _align_proteins_to_hmm(self, protein_sequences, hmm_file):
        '''hmmalign proteins to hmm, and return an alignment object

        Parameters
        ----------
        protein_sequences: generator / list of tuple(name,sequence) objects
        from SeqReader().

        '''
        cmd = "hmmalign '{}' /dev/stdin".format(hmm_file)
        output = extern.run(cmd, stdin=''.join([
            ">{}\n{}\n".format(s[0], s[1]) for s in protein_sequences]))
        protein_alignment = []
        for record in SeqIO.parse(StringIO(output), 'stockholm'):
            protein_alignment.append(AlignedProteinSequence(record.name, str(record.seq)))
        if len(protein_alignment) > 0:
            logging.debug("Read in %i aligned sequences e.g. %s %s" % (
                len(protein_alignment),
                protein_alignment[0].name,
                protein_alignment[0].seq))
        else:
            logging.debug("No aligned sequences found for this HMM")
        return protein_alignment

    def _process_taxonomically_assigned_reads(
            self,
            # inputs
            maybe_paired_readset,
            analysing_pairs,
            known_taxes,
            known_sequence_taxonomy,
            assign_taxonomy,
            singlem_assignment_method,
            assignment_result,
            output_jplace,
            known_sequence_tax,
            # outputs
            otu_table_object,
            package_to_taxonomy_bihash):

        # To deal with paired reads, process each. Then exclude second reads
        # from pairs where both match.
        if analysing_pairs:
            readset_example = maybe_paired_readset[0]
        else:
            readset_example = maybe_paired_readset
        sample_name = readset_example.sample_name
        singlem_package = readset_example.singlem_package
        
        def add_info(infos, otu_table_object, known_tax):
            for info in infos:
                to_print = [
                    singlem_package.graftm_package_basename(),
                    sample_name,
                    info.seq,
                    info.count,
                    info.coverage,
                    info.taxonomy,
                    list(sorted(info.names)),
                    info.aligned_lengths,
                    known_tax]
                otu_table_object.data.append(to_print)

        def extract_placement_parser(
                sample_name, singlem_package, tmpbase, taxonomy_bihash):
            base_dir = assignment_result._base_dir(
                sample_name, singlem_package, tmpbase)
            jplace_file = os.path.join(base_dir, "placements.jplace")
            logging.debug("Attempting to read jplace output from {}".format(
                jplace_file))
            placement_threshold = 0.5
            if os.path.exists(jplace_file):
                with open(jplace_file) as f:
                    jplace_json = json.loads(f.read())
                if analysing_pairs:
                    placement_parser = PlacementParser(
                        jplace_json, taxonomy_bihash, placement_threshold)
                else:
                    placement_parser = PlacementParser(
                        jplace_json, taxonomy_bihash, placement_threshold)
            else:
                # Sometimes alignments are filtered out.
                placement_parser = None
            return placement_parser

        def process_readset(readset, analysing_pairs):
            known_infos = self._seqs_to_counts_and_taxonomy(
                readset.known_sequences if not analysing_pairs else itertools.chain(
                    readset[0].known_sequences, readset[1].known_sequences),
                NO_ASSIGNMENT_METHOD,
                known_taxes,
                known_sequence_taxonomy,
                None)
            add_info(known_infos, otu_table_object, True)

            if not analysing_pairs and len(readset.unknown_sequences) == 0:
                return []
            elif analysing_pairs and \
                 len(readset[0].unknown_sequences) == 0 and \
                 len(readset[1].unknown_sequences) == 0:
                return []
            else: # if any sequences were aligned (not just already known)
                if assign_taxonomy:
                    if analysing_pairs:
                        aligned_seqs = list(itertools.chain(
                            readset[0].unknown_sequences, readset[0].known_sequences,
                            readset[1].unknown_sequences, readset[1].known_sequences))
                    else:
                        aligned_seqs = list(itertools.chain(
                            readset.unknown_sequences, readset.known_sequences))

                    if singlem_assignment_method == DIAMOND_EXAMPLE_BEST_HIT_ASSIGNMENT_METHOD:
                        if analysing_pairs:
                            taxonomy1 = DiamondResultParser(
                                assignment_result.forward_diamond_assignment_file(
                                    sample_name, singlem_package, readset[0].tmpfile_basename))
                            taxonomy2 = DiamondResultParser(
                                assignment_result.reverse_diamond_assignment_file(
                                    sample_name, singlem_package, readset[1].tmpfile_basename))
                            taxonomies = taxonomy2
                            taxonomies.sequence_to_hit_id.update(
                                taxonomy1.sequence_to_hit_id)
                        else:
                            tax_file = assignment_result.diamond_assignment_file(
                                sample_name, singlem_package, readset.tmpfile_basename)
                            taxonomies = DiamondResultParser(tax_file)

                    elif singlem_assignment_method == DIAMOND_ASSIGNMENT_METHOD:
                        def process_taxonomy_file(taxonomy_file_path, is_forward):
                            if not os.path.isfile(taxonomy_file_path):
                                if is_forward is None:
                                    to_add = ''
                                elif is_forward == True:
                                    to_add = ' (forward)'
                                elif is_forward == False:
                                    to_add = ' (reverse)'
                                logging.warn(
                                    "Unable to find tax file for gene {} from sample {}{} "
                                    "(likely do to min length filtering), skipping".format(
                                        os.path.basename(singlem_package.base_directory()),
                                        sample_name,
                                        to_add))
                                return None
                            else:
                                return TaxonomyFile(taxonomy_file_path)

                        if analysing_pairs:
                            taxonomy1 = process_taxonomy_file(
                                assignment_result.forward_read_tax_file(
                                    sample_name, singlem_package, readset[0].tmpfile_basename),
                                True)
                            taxonomy2 = process_taxonomy_file(
                                assignment_result.reverse_read_tax_file(
                                    sample_name, singlem_package, readset[1].tmpfile_basename),
                                False)
                            if taxonomy1 is None:
                                if taxonomy2 is None:
                                    taxonomies = {}
                                else:
                                    taxonomies = taxonomy2
                            elif taxonomy2 is None:
                                taxonomies = taxonomy1
                            else:
                                taxonomies = taxonomy1
                                taxonomies.merge(taxonomy2)

                        else:
                            taxonomies = process_taxonomy_file(
                                assignment_result.read_tax_file(
                                    sample_name, singlem_package, readset.tmpfile_basename),
                                None)
                            if taxonomies is None:
                                taxonomies = {}

                    elif singlem_assignment_method == PPLACER_ASSIGNMENT_METHOD:
                        bihash_key = singlem_package.base_directory()
                        if bihash_key in package_to_taxonomy_bihash:
                            taxonomy_bihash = package_to_taxonomy_bihash[bihash_key]
                        else:
                            taxtastic_taxonomy = singlem_package.graftm_package().taxtastic_taxonomy_path()
                            logging.debug("Reading taxtastic taxonomy from %s" % taxtastic_taxonomy)
                            with open(taxtastic_taxonomy) as f:
                                taxonomy_bihash = TaxonomyBihash.parse_taxtastic_taxonomy(f)
                            package_to_taxonomy_bihash[bihash_key] = taxonomy_bihash

                        if analysing_pairs:
                            placement_parser1 = extract_placement_parser(
                                sample_name, singlem_package, readset[0].tmpfile_basename,
                                taxonomy_bihash)
                            placement_parser2 = extract_placement_parser(
                                sample_name, singlem_package, readset[1].tmpfile_basename,
                                taxonomy_bihash)
                            if placement_parser1 is None:
                                placement_parser = placement_parser2
                            else:
                                if placement_parser2 is not None:
                                    placement_parser1.merge(placement_parser2)
                                placement_parser = placement_parser1
                        else:
                            placement_parser = extract_placement_parser(
                                sample_name, singlem_package, readset.tmpfile_basename,
                                taxonomy_bihash)
                        taxonomies = {}
                    elif singlem_assignment_method == NO_ASSIGNMENT_METHOD:
                        taxonomies = {}
                    else:
                        raise Exception("Programming error")

                else: # Taxonomy has not been assigned.
                    aligned_seqs = readset.unknown_sequences
                    if known_sequence_taxonomy:
                        taxonomies = known_sequence_tax
                    else:
                        taxonomies = {}

                new_infos = list(self._seqs_to_counts_and_taxonomy(
                    aligned_seqs, singlem_assignment_method,
                    known_sequence_tax if known_sequence_taxonomy else {},
                    taxonomies,
                    placement_parser if singlem_assignment_method == PPLACER_ASSIGNMENT_METHOD else None))

                if output_jplace:
                    if analysing_pairs:
                        raise Exception("output_jplace is not currently implemented with paired read input")
                    else:
                        base_dir = assignment_result._base_dir(
                            sample_name, singlem_package, readset.tmpfile_basename)
                        input_jplace_file = os.path.join(base_dir, "placements.jplace")
                        output_jplace_file = "%s_%s_%s.jplace" % (
                            output_jplace, sample_name, singlem_package.graftm_package_basename())
                        logging.info("Writing jplace file '%s'" % output_jplace_file)
                        logging.debug("Converting jplace file %s to singlem jplace file %s" % (
                            input_jplace_file, output_jplace_file))
                        with open(output_jplace_file, 'w') as output_jplace_io:
                            with open(input_jplace_file) as input_jplace_io:
                                self._write_jplace_from_infos(
                                    input_jplace_io, new_infos, output_jplace_io)

                return new_infos

        if analysing_pairs:
            forward_names = set([u.name for u in maybe_paired_readset[0].unknown_sequences])
            # Remove sequences from the second set when they occur in the first set
            indices_to_remove = []
            for i, u in enumerate(maybe_paired_readset[1].unknown_sequences):
                if u.name in forward_names:
                    logging.debug("Removing sequence '{}' from the set of aligned reverse reads".format(
                        u.name))
                    indices_to_remove.append(i)
            for i in reversed(indices_to_remove):
                del maybe_paired_readset[1].unknown_sequences[i]
            logging.debug(
                "Removed {} sequences from reverse read set as the forward read was also detected".format(
                    len(indices_to_remove)))

        new_infos = process_readset(maybe_paired_readset, analysing_pairs)
        add_info(new_infos, otu_table_object, not assign_taxonomy)



    def _seqs_to_counts_and_taxonomy(self, sequences,
                                     assignment_method,
                                     otu_sequence_assigned_taxonomies,
                                     per_read_taxonomies,
                                     placement_parser):
        '''Given an array of UnalignedAlignedNucleotideSequence objects, and taxonomic
        assignment-related results, yield over 'Info' objects that contain e.g.
        the counts of the aggregated sequences and corresponding median
        taxonomies.

        Parameters
        ----------
        sequences: iterable of UnalignedAlignedNucleotideSequence
        assignment_method: str
            e.g. DIAMOND_EXAMPLE_BEST_HIT_ASSIGNMENT_METHOD
        otu_sequence_assigned_taxonomies: dict of str to str
            assignments known based on the OTU sequence alone
        per_read_taxonomies: dict-like of read name to taxonomy
        placement_parser: PlacementParser
            Used only if assignment_method is PPLACER_ASSIGNMENT_METHOD.
        '''
        class CollectedInfo:
            def __init__(self):
                self.count = 0
                self.taxonomies = []
                self.names = []
                self.coverage = 0.0
                self.aligned_lengths = []
                self.orf_names = []
                self.known_sequence_taxonomies = []

        seq_to_collected_info = {}
        for s in sequences:
            if s.aligned_sequence in otu_sequence_assigned_taxonomies or \
               per_read_taxonomies is None:
                tax = None
            else:
                try:
                    tax = per_read_taxonomies[s.name]
                except KeyError:
                    if assignment_method != NO_ASSIGNMENT_METHOD and \
                       assignment_method != PPLACER_ASSIGNMENT_METHOD:
                        # happens sometimes when HMMER picks up something where
                        # diamond does not, or when --no_assign_taxonomy is specified.
                        logging.debug("Did not find any taxonomy information for %s" % s.name)
                        tax = ''

            try:
                collected_info = seq_to_collected_info[s.aligned_sequence]
            except KeyError:
                collected_info = CollectedInfo()
                seq_to_collected_info[s.aligned_sequence] = collected_info

            collected_info.count += 1
            if per_read_taxonomies: collected_info.taxonomies.append(tax)
            collected_info.names.append(s.name)
            collected_info.coverage += s.coverage_increment()
            collected_info.aligned_lengths.append(s.aligned_length)
            collected_info.orf_names.append(s.orf_name)

        class Info:
            def __init__(self, seq, count, taxonomy, names, coverage, aligned_lengths):
                self.seq = seq
                self.count = count
                self.taxonomy = taxonomy
                self.names = names
                self.coverage = coverage
                self.aligned_lengths = aligned_lengths

        for seq, collected_info in seq_to_collected_info.items():
            if s.aligned_sequence in otu_sequence_assigned_taxonomies:
                tax = otu_sequence_assigned_taxonomies[s.aligned_sequence].taxonomy
            elif assignment_method == DIAMOND_EXAMPLE_BEST_HIT_ASSIGNMENT_METHOD:
                tax = collected_info.taxonomies[0]
                if tax is None: tax = ''
            elif assignment_method == PPLACER_ASSIGNMENT_METHOD and placement_parser is not None:
                placed_tax = placement_parser.otu_placement(
                    collected_info.orf_names)
                if placed_tax is None:
                    tax = ''
                else:
                    tax = '; '.join(placed_tax)
            elif per_read_taxonomies is None:
                tax = ''
            else:
                tax = self._median_taxonomy(collected_info.taxonomies)

            yield Info(seq,
                       collected_info.count,
                       tax,
                       collected_info.names,
                       collected_info.coverage,
                       collected_info.aligned_lengths)



    def _median_taxonomy(self, taxonomies):
        levels_to_counts = []
        for tax_string in taxonomies:
            for i, tax in enumerate(tax_string.split(';')):
                tax = tax.strip()
                if i >= len(levels_to_counts):
                    levels_to_counts.append({})
                try:
                    levels_to_counts[i][tax] += 1
                except KeyError:
                    levels_to_counts[i][tax] = 1

        median_tax = []
        for level_counts in levels_to_counts:
            max_count = 0
            max_tax = None
            for tax, count in level_counts.items():
                if count > max_count:
                    max_count = count
                    max_tax = tax
            if float(max_count) / len(taxonomies) > 0.5:
                median_tax.append(max_tax)
            else:
                break
        return '; '.join(median_tax)

    def _write_jplace_from_infos(self, input_jplace_io, infos, output_jplace_io):

        jplace = json.load(input_jplace_io)
        if jplace['version'] != 3:
            raise Exception("SingleM currently only works with jplace version 3 files, sorry")

        name_to_info = {}
        for info in infos:
            for name in info.names:
                name_to_info[name] = info

        # rewrite placements to be OTU-wise instead of sequence-wise
        orfm_utils = OrfMUtils()
        another_regex = re.compile(r'_\d+$')
        sequence_to_count = {}
        sequence_to_example_p = {}

        for placement in jplace['placements']:
            if 'nm' not in placement:
                raise Exception("Unexpected jplace format detected in placement %s" % placement)
            for name_and_count in placement['nm']:
                if len(name_and_count) != 2:
                    raise Exception("Unexpected jplace format detected in nm %s" % name_and_count)
                name, count = name_and_count
                real_name = another_regex.sub('', orfm_utils.un_orfm_name(name))
                info = name_to_info[real_name]
                sequence = info.seq

                try:
                    sequence_to_count[sequence] += count
                except KeyError:
                    sequence_to_count[sequence] = count

                if real_name == info.names[0] and \
                   sequence not in sequence_to_example_p: # For determinism:
                    sequence_to_example_p[sequence] = placement['p']

        new_placements = {}
        for sequence, example_p in sequence_to_example_p.items():
            new_placements[sequence] = {}
            new_placements[sequence]['nm'] = [[sequence, sequence_to_count[sequence]]]
            new_placements[sequence]['p'] = example_p

        jplace['placements'] = list(new_placements.values())
        json.dump(jplace, output_jplace_io)

    def _graftm_command_prefix(self, is_protein):
        # --min_orf_length is unused for nucleotide HMMs but does no harm.
        cmd = "graftM graft "\
              "--verbosity %s "\
              "--input_sequence_type nucleotide " % self._graftm_verbosity
        if self._evalue: cmd += ' --evalue %s' % self._evalue
        if self._restrict_read_length: cmd += ' --restrict_read_length %i' % self._restrict_read_length

        if is_protein:
            cmd += " --min_orf_length %s " % self._min_orf_length
            if self._filter_minimum_protein:
                cmd += "  --filter_minimum %i" % self._filter_minimum_protein
        elif self._filter_minimum_nucleotide:
            cmd += "  --filter_minimum %i" % self._filter_minimum_nucleotide

        return cmd+' '
    
    def _prefilter(self, diamond_database, read_files, reverse=False):
        '''Find all reads that match the DIAMOND database in the 
        singlem_package database.
        Parameters
        ----------
        diamond_database: dmnd 
            DIAMOND database from the SingleM packages to search reads with
        read_files: list of str 
            paths to the sequences to be searched
        reverse: boolean
            check if using reverse reads
        Returns
        -------
        path to fasta file of filtered reads
        '''
        filtered_reads = []
        if reverse:
            # running on reverse reads
            prefilter_dir = os.path.join(self._working_directory, 'prefilter_reverse')
        else:
            prefilter_dir = os.path.join(self._working_directory, 'prefilter_forward')
        os.mkdir(prefilter_dir)
        
        for file in read_files:
            fasta_path = os.path.join(prefilter_dir,
                                      os.path.basename(file))
            if fasta_path[-3:] == '.gz':
                fasta_path = fasta_path[:-3] # remove .gz for destination files
            fasta_path = os.path.splitext(fasta_path)[0]+'.fna'
            
            f = open(fasta_path, 'w+') # create tempfile in working directory
            f.close()
            
            cmd = "zcat -f %s | " \
                  "diamond blastx " \
                  "--outfmt 6 qseqid full_qseq " \
                  "--max-target-seqs 1 " \
                  "--evalue 0.01 " \
                  "--index-chunks 1 " \
                  "--threads %i " \
                  "--query - " \
                  "--db %s " \
                  "| sed -e 's/^/>/' -e 's/\\t/\\n/' > %s" % (
                      file,
                      self._num_threads,
                      diamond_database,
                      fasta_path)
            extern.run(cmd)
            filtered_reads.append(fasta_path)
            
        return filtered_reads

    def _search(self, singlem_package_database, forward_read_files, reverse_read_files):
        '''Find all reads that match one or more of the search HMMs in the
        singlem_package_database.
        Parameters
        ----------
        singlem_package_database: HmmDatabase
            packages to search the reads for
        forward_read_files: list of str
            paths to the sequences to be searched
        reverse_read_files: list of str or None
            paths to the reverse sequences to be searched, or None to run in
            unpaired mode. Must be the same length as forward_read_files unless
            None.
        Returns
        -------
        SingleMPipeSearchResult
        '''
        graftm_protein_search_directory = os.path.join(
            self._working_directory, 'graftm_protein_search')
        graftm_nucleotide_search_directory = os.path.join(
            self._working_directory, 'graftm_nucleotide_search')

        def run(hmm_paths, output_directory, is_protein):
            cmd = self._graftm_command_prefix(is_protein) + \
                  "--threads %i "\
                  "--forward %s "\
                  "--search_only "\
                  "--search_hmm_files %s "\
                  "--output_directory %s "\
                  "--aln_hmm_file %s " % (
                      self._num_threads,
                      ' '.join(forward_read_files),
                      ' '.join(hmm_paths),
                      output_directory,
                      hmm_paths[0])
            if reverse_read_files is not None:
                cmd += "--reverse {} ".format(
                    ' '.join(reverse_read_files))
            extern.run(cmd)

        num_singlem_packages = len(singlem_package_database.protein_packages())+\
                               len(singlem_package_database.nucleotide_packages())
        logging.info("Searching with %i SingleM package(s)" % num_singlem_packages)
        
        # Run searches for proteins
        hmms = singlem_package_database.protein_search_hmm_paths()
        doing_proteins = False
        if len(hmms) > 0:
            doing_proteins = True
            logging.info("Searching for reads matching %i different protein HMM(s)" % len(hmms))
            run(hmms, graftm_protein_search_directory, True)

        # Run searches for nucleotides
        hmms = singlem_package_database.nucleotide_search_hmm_paths()
        doing_nucs = False
        if len(hmms) > 0:
            doing_nucs = True
            logging.info("Searching for reads matching %i different nucleotide HMM(s)" % len(hmms))
            run(hmms, graftm_nucleotide_search_directory, False)

        logging.info("Finished search phase")
        analysing_pairs = reverse_read_files is not None
        protein_graftm = GraftMResult(graftm_protein_search_directory, analysing_pairs, search_hmm_files=hmms) if \
                         doing_proteins else None
        nuc_graftm = GraftMResult(graftm_nucleotide_search_directory, analysing_pairs, search_hmm_files=hmms) if \
                     doing_nucs else None
        return SingleMPipeSearchResult(
            protein_graftm, nuc_graftm, analysing_pairs)

    def _align(self, search_result):
        graftm_separate_directory_base = os.path.join(self._working_directory, 'graftm_separates')
        os.mkdir(graftm_separate_directory_base)
        logging.info("Running separate alignments in GraftM..")
        commands = []

        def command(singlem_package, hit_files, is_protein, analysing_pairs):
            cmd = self._graftm_command_prefix(is_protein) + \
                "--threads %i "\
                "--graftm_package %s --output_directory %s/%s "\
                "--search_only" % (
                    1, #use 1 thread since most likely better to parallelise processes with extern
                    singlem_package.graftm_package_path(),
                    graftm_separate_directory_base,
                    os.path.basename(singlem_package.graftm_package_path()))
            if analysing_pairs:
                cmd += ' --forward {} --reverse {}'.format(
                    ' '.join([h[0] for h in hit_files]),
                    ' '.join([h[1] for h in hit_files]))
            else:
                cmd += ' --forward {}'.format(
                    ' '.join(hit_files))
            return cmd

        # Gather commands for aligning protein packages
        analysing_pairs = search_result.analysing_pairs
        for singlem_package in self._singlem_package_database.protein_packages():
            commands.append(command(
                singlem_package,
                list(search_result.protein_hit_paths().values()),
                True,
                analysing_pairs))
        # Gather commands for aligning nucleotide packages.
        for singlem_package in self._singlem_package_database.nucleotide_packages():
            temporary_hit_files = [tf for _, tf in \
                search_result.direction_corrected_nucleotide_read_files()]
            commands.append(command(
                singlem_package,
                temporary_hit_files,
                False,
                analysing_pairs))

        extern.run_many(commands, num_threads=self._num_threads)
        return SingleMPipeAlignSearchResult(
            graftm_separate_directory_base,
            search_result.samples_with_hits(),
            analysing_pairs)

    def _assign_taxonomy(self, extracted_reads, assignment_method):
        graftm_align_directory_base = os.path.join(self._working_directory, 'graftm_aligns')
        os.mkdir(graftm_align_directory_base)
        commands = []

        def generate_tempfile_for_readset(readset):
            tmp = tempfile.NamedTemporaryFile(
                mode='w',
                prefix='singlem.%s' % readset.sample_name, suffix=".fasta",
                delete=False)
            # Record basename (remove .fasta) so that the graftm output
            # file is recorded for later on in pipe.
            tmpbase = os.path.basename(tmp.name[:-6])
            readset.tmpfile_basename = tmpbase
            return tmp

        # Run each one at a time serially so that the number of threads is
        # respected, to save RAM as one DB needs to be loaded at once, and so
        # fewer open files are needed, so that the open file count limit is
        # eased.
        seqio = SequenceIO()
        for singlem_package, readsets in extracted_reads.each_package_wise():
            tmp_files = []
            for readset in readsets:
                if extracted_reads.analysing_pairs:
                    if len(readset[0].sequences + readset[1].sequences) > 0:
                        # Some pairs will only have one side of the pair
                        # aligned, some pairs both. Fill in the forward and
                        # reverse files with dummy data as necessary
                        #
                        # The dummy sequence must have an ORF with
                        # >min_orf_length bases because otherwise if there are
                        # >no sequences, hmmsearch inside graftm croaks.
                        dummy_sequence = 'ATG'+''.join(['A']*self._min_orf_length)
                        forward_tmp = generate_tempfile_for_readset(readset[0])
                        reverse_tmp = generate_tempfile_for_readset(readset[1])

                        forward_seq_names = {}
                        for (i, s) in enumerate(readset[0].sequences):
                            forward_seq_names[s.name] = i
                            seqio.write_fasta([s], forward_tmp)
                        reverse_name_to_seq = {}
                        for s in readset[1].sequences:
                            reverse_name_to_seq[s.name] = s
                        for name, forward_i in forward_seq_names.items():
                            if name in reverse_name_to_seq:
                                # Write corresponding reverse and delete it
                                # from dict.
                                seqio.write_fasta(
                                    [reverse_name_to_seq.pop(name)], reverse_tmp)
                            else:
                                # Forward read matched only
                                reverse_tmp.write(">{}\n{}\n".format(
                                    name, dummy_sequence))
                        for name, seq in reverse_name_to_seq.items():
                            # Reverse read matched only
                            forward_tmp.write(">{}\n{}\n".format(
                                name, dummy_sequence))
                            seqio.write_fasta([seq], reverse_tmp)

                        # Close immediately to avoid the "too many open files" error.
                        forward_tmp.close()
                        reverse_tmp.close()
                        tmp_files.append([forward_tmp, reverse_tmp])
                else:
                    if len(readset.sequences) > 0:
                        tmp = generate_tempfile_for_readset(readset)
                        seqio.write_fasta(readset.sequences, tmp)
                        tmp_files.append(tmp)
                        # Close immediately to avoid the "too many open files" error.
                        tmp.close()

            if len(tmp_files) > 0:
                cmd = "%s "\
                      "--threads %i "\
                      "--graftm_package %s "\
                      "--max_samples_for_krona 0 "\
                      "--assignment_method %s " % (
                          self._graftm_command_prefix(singlem_package.is_protein_package()),
                          self._num_threads,
                          singlem_package.graftm_package_path(),
                          assignment_method)
                if extracted_reads.analysing_pairs:
                    if assignment_method == PPLACER_ASSIGNMENT_METHOD:
                        cmd += "--output_directory {}/{} ".format(
                            graftm_align_directory_base,
                            singlem_package.graftm_package_basename())
                        cmd += " --forward {} --reverse {}".format(
                            ' '.join(t[0].name for t in tmp_files),
                            ' '.join(t[1].name for t in tmp_files))
                        commands.append(cmd)
                    elif assignment_method == DIAMOND_ASSIGNMENT_METHOD:
                        # GraftM ignores reverse reads with diamond assignment
                        # method, so run forward and reverse individually.
                        cmd1 = cmd + "--forward {} --output_directory {}".format(
                            ' '.join(t[0].name for t in tmp_files),
                            self._diamond_assign_taxonomy_paired_output_directory(
                                graftm_align_directory_base, singlem_package, True))
                        cmd2 = cmd + "--forward {} --output_directory {}".format(
                            ' '.join(t[1].name for t in tmp_files),
                            self._diamond_assign_taxonomy_paired_output_directory(
                                graftm_align_directory_base, singlem_package, False))
                        commands.append(cmd1)
                        commands.append(cmd2)
                else:
                    cmd += "--output_directory {}/{} ".format(
                        graftm_align_directory_base,
                        singlem_package.graftm_package_basename())
                    tmpnames = list([tg.name for tg in tmp_files])
                    cmd += " --forward {} ".format(
                        ' '.join(tmpnames))
                    commands.append(cmd)

        extern.run_many(commands, num_threads=1)
        logging.info("Finished running taxonomic assignment with GraftM")
        return SingleMPipeTaxonomicAssignmentResult(graftm_align_directory_base)

    def _diamond_assign_taxonomy_paired_output_directory(
            self, graftm_align_directory_base, singlem_package, is_forward):
        return "{}/{}_{}".format(
            graftm_align_directory_base,
            singlem_package.graftm_package_basename(),
            "read1" if is_forward else "read2")

class SingleMPipeSearchResult:
    def __init__(self, graftm_protein_result, graftm_nucleotide_result, analysing_pairs):
        self._protein_result = graftm_protein_result
        self._nucleotide_result = graftm_nucleotide_result
        self.analysing_pairs = analysing_pairs

    def protein_hit_paths(self):
        '''Return a dict of sample name to corresponding '_hits.fa' files generated in
        the search step. Do not return those samples where there were no hits.

        If analysing paired data, return an pair of paths (fwd, rev) as the
        values in the Dict.

        '''
        if self._protein_result is None:
            return {}
        else:
            return self._protein_result.unaligned_sequence_paths(require_hits=True)

    def direction_corrected_nucleotide_read_files(self):
        '''For nucleotide HMMs: Iterate over the sample names plus a fasta filename per
        sample, fasta files that are 'direction-corrected' i.e. contain
        sequences in the direction that they were aligned. These tempfiles must
        be closed by code using this function. Do not use this method for
        protein HMMs.

        '''
        if self._nucleotide_result is None:
            # No nucleotide singlem packages
            return
        for sample_name in self._nucleotide_result.sample_names(require_hits=True):
            forward_read_to_score = {}
            reverse_read_to_score = {}
            for hmmout in self._nucleotide_result.hmmout_paths_from_sample_name(sample_name):
                hmmout_result = HMMSearchResult.import_from_nhmmer_table(hmmout)
                for hit in hmmout_result.each(
                        [SequenceSearchResult.QUERY_ID_FIELD,
                         SequenceSearchResult.ALIGNMENT_DIRECTION,
                         SequenceSearchResult.ALIGNMENT_BIT_SCORE]):
                    name = hit[0]
                    score = float(hit[2])
                    is_poorer_score = False
                    if name in forward_read_to_score:
                        if score > forward_read_to_score[name]:
                            del forward_read_to_score[name]
                        else:
                            is_poorer_score = True
                    if name in reverse_read_to_score:
                        if score > reverse_read_to_score[name]:
                            del reverse_read_to_score[name]
                        else:
                            is_poorer_score = True
                    if not is_poorer_score:
                        if hit[1]:
                            forward_read_to_score[name] = score
                        else:
                            reverse_read_to_score[name] = score
            nucs = self._nucleotide_result.unaligned_sequences_path_from_sample_name(sample_name)

            yieldme = os.path.join(self._nucleotide_result.output_directory,
                                   "%s_hits.fa" % sample_name)
            SequenceExtractor().extract_forward_and_reverse_complement(
                forward_read_to_score.keys(), reverse_read_to_score.keys(), nucs, yieldme)
            if os.stat(yieldme).st_size > 0:
                yield sample_name, yieldme

    def samples_with_hits(self):
        '''Return a list of sample names that had at least one hit'''
        return list(set(itertools.chain(
            self.protein_hit_paths().keys(),
            self._nucleotide_result.sample_names(require_hits=True) if
                self._nucleotide_result else [])))

class SingleMPipeAlignSearchResult:
    def __init__(
            self,
            graftm_separate_directory_base,
            sample_names,
            analysing_pairs):
        self._graftm_separate_directory_base = graftm_separate_directory_base
        self._sample_names = sample_names
        self.analysing_pairs = analysing_pairs

    def _base_dir(self, sample_name, singlem_package):
        if self.analysing_pairs:
            return [
                os.path.join(
                    self._graftm_separate_directory_base,
                    os.path.basename(singlem_package.graftm_package_path()),
                    '{}_forward_hits'.format(sample_name),
                    'forward'),
                os.path.join(
                    self._graftm_separate_directory_base,
                    os.path.basename(singlem_package.graftm_package_path()),
                    '{}_forward_hits'.format(sample_name),
                    'reverse')]
        else:
            return os.path.join(
                self._graftm_separate_directory_base,
                os.path.basename(singlem_package.graftm_package_path()),
                '%s_hits' % sample_name)

    def prealigned_sequence_files(self, sample_name, singlem_package):
        '''Yield a path to the sequences that were aligned to the HMM.

        '''
        if singlem_package.is_protein_package():
            if self.analysing_pairs:
                base_dirs = self._base_dir(sample_name, singlem_package)
                yield [
                    os.path.join(
                        base_dirs[0],
                        "{}_forward_hits_forward_orf.fa".format(sample_name)),
                    os.path.join(
                        base_dirs[1],
                        "{}_forward_hits_reverse_orf.fa".format(sample_name)),
                ]
            else:
                yield os.path.join(
                    self._base_dir(sample_name, singlem_package),
                    "%s_hits_orf.fa" % sample_name)
        else:
            yield self.nucleotide_sequence_file(sample_name, singlem_package)

    def nucleotide_sequence_file(self, sample_name, singlem_package):
        if self.analysing_pairs:
            base_dirs = self._base_dir(sample_name, singlem_package)
            return [
                os.path.join(
                    base_dirs[0],
                    "{}_forward_hits_forward_hits.fa".format(sample_name)),
                os.path.join(
                    base_dirs[1],
                    "{}_forward_hits_reverse_hits.fa".format(sample_name)),
            ]
        else:
            return os.path.join(
                self._base_dir(sample_name, singlem_package),
                "%s_hits_hits.fa" % sample_name)

    def sample_names(self):
        return self._sample_names

class SingleMPipeTaxonomicAssignmentResult:
    def __init__(self, graftm_output_directory):
        self._graftm_output_directory = graftm_output_directory

    def _base_dir(self, sample_name, singlem_package, tmpbase):
        return os.path.join(self._graftm_output_directory,
                            '%s/%s' % (
                                singlem_package.graftm_package_basename(),
                                re.sub('\.fasta$','',tmpbase)))

    def _base_dir1(self, sample_name, singlem_package, tmpbase):
        return os.path.join(
            self._graftm_output_directory,
            "{}_read1".format(singlem_package.graftm_package_basename()),
            tmpbase)

    def _base_dir2(self, sample_name, singlem_package, tmpbase):
        return os.path.join(
            self._graftm_output_directory,
            "{}_read2".format(singlem_package.graftm_package_basename()),
            tmpbase)

    def protein_orf_file(self, sample_name, singlem_package, tmpbase):
        return os.path.join(self._base_dir(sample_name, singlem_package, tmpbase),
                            "%s_orf.fa" % tmpbase)

    def prealigned_sequence_file(self, sample_name, singlem_package, tmpbase):
        '''path to the sequences that were aligned (ORF for proteins, regular seqs for
        nucleotide).

        '''
        if singlem_package.is_protein_package():
            return self.protein_orf_file(sample_name, singlem_package, tmpbase)
        else:
            return self.nucleotide_hits_file(sample_name, singlem_package, tmpbase)

    def nucleotide_hits_file(self, sample_name, singlem_package, tmpbase):
        return os.path.join(self._base_dir(sample_name, singlem_package, tmpbase),
                            "%s_hits.fa" % tmpbase)

    def diamond_assignment_file(self, sample_name, singlem_package, tmpbase):
        return os.path.join(self._base_dir(sample_name, singlem_package, tmpbase),
                            '%s_diamond_assignment.daa' % tmpbase)

    def forward_diamond_assignment_file(self, sample_name, singlem_package, tmpbase):
        return os.path.join(self._base_dir1(sample_name, singlem_package, tmpbase),
                            '{}_diamond_assignment.daa'.format(tmpbase))

    def reverse_diamond_assignment_file(self, sample_name, singlem_package, tmpbase):
        return os.path.join(self._base_dir2(sample_name, singlem_package, tmpbase),
                            '{}_diamond_assignment.daa'.format(tmpbase))

    def read_tax_file(self, sample_name, singlem_package, tmpbase):
        return os.path.join(self._base_dir(sample_name, singlem_package, tmpbase),
                            '%s_read_tax.tsv' % tmpbase)

    def forward_read_tax_file(self, sample_name, singlem_package, tmpbase):
        return os.path.join(self._base_dir1(sample_name, singlem_package, tmpbase),
                            '{}_read_tax.tsv'.format(tmpbase))

    def reverse_read_tax_file(self, sample_name, singlem_package, tmpbase):
        return os.path.join(self._base_dir2(sample_name, singlem_package, tmpbase),
                            '{}_read_tax.tsv'.format(tmpbase))

    def jplace_file(self, sample_name, singlem_package, tmpbase):
        return os.path.join(self._base_dir(sample_name, singlem_package, tmpbase),
                            'placements.jplace')

class ExtractedReads:
    '''Collection class for ExtractedReadSet objects'''
    def __init__(self, analysing_pairs):
        self._sample_to_extracted_read_objects = {}
        self.analysing_pairs = analysing_pairs

    def add(self, extracted_read_set):
        '''Add an ExtractedReadSet, or if analysing_pairs, a pair of them'''
        if self.analysing_pairs:
            sample_name = extracted_read_set[0].sample_name
        else:
            sample_name = extracted_read_set.sample_name

        if sample_name in self._sample_to_extracted_read_objects:
            self._sample_to_extracted_read_objects[sample_name].append(extracted_read_set)
        else:
            self._sample_to_extracted_read_objects[sample_name] = [extracted_read_set]

    def __iter__(self):
        '''yield sample, extracted_read_sets'''
        for readsets in self._sample_to_extracted_read_objects.values():
            for readset in readsets:
                yield readset

    def each_package_wise(self):
        '''yield once per pkg: [singlem_package, ExtractedReadSet objects with all
        samples / sequences that have extracted sequences from it]

        '''
        pkg_basename_to_readsets = {}
        for sample, readsets in self._sample_to_extracted_read_objects.items():
            for readset in readsets:
                if self.analysing_pairs:
                    pkg_base = readset[0].singlem_package.base_directory()
                else:
                    pkg_base = readset.singlem_package.base_directory()
                if pkg_base in pkg_basename_to_readsets:
                    pkg_basename_to_readsets[pkg_base].append(readset)
                else:
                    pkg_basename_to_readsets[pkg_base] = [readset]
        for readsets in pkg_basename_to_readsets.values():
            if self.analysing_pairs:
                yield readsets[0][0].singlem_package, readsets
            else:
                yield readsets[0].singlem_package, readsets

    def each_sample(self):
        return self._sample_to_array.keys()

class ExtractedReadSet:
    def __init__(self, sample_name, singlem_package, sequences,
                 known_sequences, unknown_sequences):
        self.sample_name = sample_name
        self.singlem_package = singlem_package
        self.sequences = sequences
        self.known_sequences = known_sequences
        self.unknown_sequences = unknown_sequences
        self.tmpfile_basename = None # Used as part of pipe, making this object
                                     # not suitable for use outside that
                                     # setting.
