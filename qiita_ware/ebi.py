from os.path import basename, join, isdir, isfile
from os import makedirs, remove, listdir
from datetime import date, timedelta
from urllib import quote
from xml.etree import ElementTree as ET
from xml.etree.ElementTree import ParseError
from xml.sax.saxutils import escape
from gzip import GzipFile
from functools import partial
from h5py import File
from future.utils import viewitems, viewkeys
from skbio.util import safe_md5, create_dir

from qiita_core.qiita_settings import qiita_config
from qiita_ware.exceptions import EBISubmissionError
from qiita_ware.demux import to_per_sample_ascii
from qiita_ware.util import open_file
from qiita_db.logger import LogEntry
from qiita_db.ontology import Ontology
from qiita_db.util import convert_to_id, get_mountpoint
from qiita_db.study import Study
from qiita_db.data import PreprocessedData
from qiita_db.metadata_template import PrepTemplate, SampleTemplate


def clean_whitespace(text):
    """Standardizes whitespaces so there is only one space separating tokens

    Parameters
    ----------
    text : str
        The fixed text

    Returns
    -------
    str
        fixed text
    """
    return ' '.join(unicode(str(text), 'utf8').split())


class EBISubmission(object):
    """Define an EBI submission, generate submission files and submit

    Submit a preprocessed data to EBI

    The steps for EBI submission are:
    1. Validate that we have all required info to submit
    2. Generate per sample demultiplexed files
    3. Generate XML files for submission
    4. Submit sequences files
    5. Submit XML files. The answer has the EBI submission numbers.

    Parameters
    ----------
    preprocessed_data_id : int
        The preprocesssed data id to submit
    action : str
        The action to perform. Valid options see
        EBISubmission.valid_ebi_actions

    Raises
    ------
    EBISubmissionError
        - If the action is not in EBISubmission.valid_ebi_actions
        - If the preprocessed data has been already submitted to EBI
        - If the status of the study attached to the preprocessed data is
        submitting
        - If the prep template investigation type is not in the
        ena_ontology.terms or not in the ena_ontology.user_defined_terms
        - If the submission is missing required EBI fields either in the sample
        or prep template
        - If the sample preparation metadata doesn't have a platform field or
        it isn't a EBISubmission.valid_platforms
    """

    valid_ebi_actions = ('ADD', 'VALIDATE', 'MODIFY')
    valid_ebi_submission_states = ('submitting')
    # valid_platforms dict of 'platform': ['valid_instrument_models']
    valid_platforms = {'LS454': ['454 GS', '454 GS 20', '454 GS FLX',
                                 '454 GS FLX+', '454 GS FLX TITANIUM',
                                 '454 GS JUNIOR', 'UNSPECIFIED'],
                       'ILLUMINA': ['ILLUMINA GENOME ANALYZER',
                                    'ILLUMINA GENOME ANALYZER II',
                                    'ILLUMINA GENOME ANALYZER IX',
                                    'ILLUMINA HISEQ 2500',
                                    'ILLUMINA HISEQ 2000',
                                    'ILLUMINA HISEQ 1500',
                                    'ILLUMINA HISEQ 1000', 'ILLUMINA MISEQ',
                                    'ILLUMINA HISCANSQ', 'HISEQ X TEN',
                                    'NEXTSEQ 500', 'UNSPECIFIED']}
    xmlns_xsi = "http://www.w3.org/2001/XMLSchema-instance"
    xsi_noNSL = "ftp://ftp.sra.ebi.ac.uk/meta/xsd/sra_1_3/SRA.%s.xsd"
    experiment_library_fields = ['library_strategy']

    def __init__(self, preprocessed_data_id, action):
        error_msgs = []

        if action not in self.valid_ebi_actions:
            error_msg = ("%s is not a valid EBI submission action, valid "
                         "actions are: %s" %
                         (action, ', '.join(self.valid_ebi_actions)))
            LogEntry.create('Runtime', error_msg)
            raise EBISubmissionError(error_msg)

        ena_ontology = Ontology(convert_to_id('ENA', 'ontology'))
        self.action = action
        self.preprocessed_data = PreprocessedData(preprocessed_data_id)
        self.study = Study(self.preprocessed_data.study)
        self.sample_template = SampleTemplate(self.study.sample_template)
        self.prep_template = PrepTemplate(self.preprocessed_data.prep_template)

        if self.preprocessed_data.is_submitted_to_ebi:
            error_msg = ("Cannot resubmit! Preprocessed data %d has already "
                         "been submitted to EBI.")
            LogEntry.create('Runtime', error_msg)
            raise EBISubmissionError(error_msg)

        status = self.study.ebi_submission_status
        if status in self.valid_ebi_submission_states:
            error_msg = ("Cannot perform parallel EBI submission for the same "
                         "study. Current status of the study: %s" % status)
            LogEntry.create('Runtime', error_msg)
            raise EBISubmissionError(error_msg)

        self.preprocessed_data_id = preprocessed_data_id
        self.study_title = self.study.title
        self.study_abstract = self.study.info['study_abstract']

        it = self.prep_template.investigation_type
        if it in ena_ontology.terms:
            self.investigation_type = it
            self.new_investigation_type = None
        elif it in ena_ontology.user_defined_terms:
            self.investigation_type = 'Other'
            self.new_investigation_type = it
        else:
            # This should never happen
            error_msgs.append("Unrecognized investigation type: '%s'. This "
                              "term is neither one of the official terms nor "
                              "one of the user-defined terms in the ENA "
                              "ontology." % it)
        _, base_fp = get_mountpoint("preprocessed_data")[0]
        self.ebi_dir = '%d_ebi_submission' % preprocessed_data_id
        self.full_ebi_dir = join(base_fp, self.ebi_dir)
        self.ascp_reply = join(self.full_ebi_dir, 'ascp_reply.txt')
        self.curl_reply = join(self.full_ebi_dir, 'curl_reply.xml')
        self.xml_dir = join(self.full_ebi_dir, 'xml_dir')
        self.study_xml_fp = None
        self.sample_xml_fp = None
        self.experiment_xml_fp = None
        self.run_xml_fp = None
        self.submission_xml_fp = None
        self.pmids = self.study.pmids

        # getting the restrictions
        st_missing = self.sample_template.check_restrictions(
            [self.sample_template.columns_restrictions['EBI']])
        pt_missing = self.prep_template.check_restrictions(
            [self.prep_template.columns_restrictions['EBI']])
        # testing if there are any missing columns
        if st_missing:
            error_msgs.append("Missing column in the sample template: %s" %
                              ', '.join(list(st_missing)))
        if pt_missing:
            error_msgs.append("Missing column in the prep template: %s" %
                              ', '.join(list(pt_missing)))

        # generating all samples from sample template
        self.samples = {}
        self.samples_prep = {}
        self.sample_demux_fps = {}
        get_output_fp = partial(join, self.full_ebi_dir)
        nvp = []
        nvim = []
        for k, v in viewitems(self.sample_template):
            if k not in self.prep_template:
                continue
            sample_prep = self.prep_template[k]

            # validating required fields
            if ('platform' not in sample_prep or
                    sample_prep['platform'] is None):
                nvp.append(k)
            else:
                platform = sample_prep['platform'].upper()
                if platform not in self.valid_platforms:
                    nvp.append(k)
                else:
                    if ('instrument_model' not in sample_prep or
                            sample_prep['instrument_model'] is None):
                        nvim.append(k)
                    else:
                        im = sample_prep['instrument_model'].upper()
                        if im not in self.valid_platforms[platform]:
                            nvim.append(k)

            self.samples[k] = v
            self.samples_prep[k] = sample_prep
            self.sample_demux_fps[k] = get_output_fp("%s.fastq.gz" % k)

        if nvp:
            error_msgs.append("These samples do not have a valid platform "
                              "(instrumet model wasn't checked): %s" % (
                                  ', '.join(nvp)))
        if nvim:
            error_msgs.append("These samples do not have a valid instrument "
                              "model: %s" % (', '.join(nvim)))
        if error_msgs:
            error_msgs = ("Errors found during EBI submission for study #%d, "
                          "preprocessed data #%d and prep template #%d:\n%s"
                          % (self.study.id, preprocessed_data_id,
                             self.prep_template.id, '\n'.join(error_msgs)))
            LogEntry.create('Runtime', error_msgs)
            raise EBISubmissionError(error_msgs)

        self._sample_aliases = {}
        self._experiment_aliases = {}
        self._run_aliases = {}

        self._ebi_sample_accessions = \
            self.sample_template.ebi_sample_accessions
        self._ebi_experiment_accessions = \
            self.prep_template.ebi_experiment_accessions

    def _get_study_alias(self):
        """Format alias using ``self.preprocessed_data_id``"""
        study_alias_format = '%s_sid_%s'
        return study_alias_format % (
            qiita_config.ebi_organization_prefix,
            escape(clean_whitespace(str(self.study.id))))

    def _get_sample_alias(self, sample_name):
        """Format alias using ``self.preprocessed_data_id``, `sample_name`"""
        alias = "%s:%s" % (self._get_study_alias(),
                           escape(clean_whitespace(str(sample_name))))
        self._sample_aliases[alias] = sample_name
        return alias

    def _get_experiment_alias(self, sample_name):
        """Format alias using ``self.preprocessed_data_id``, and `sample_name`

        Currently, this is identical to _get_sample_alias above, since we are
        only going to allow submission of one prep for each sample
        """
        exp_alias_format = '%s_ptid_%s:%s'
        alias = exp_alias_format % (
            qiita_config.ebi_organization_prefix,
            escape(clean_whitespace(str(self.prep_template.id))),
            escape(clean_whitespace(str(sample_name))))
        self._experiment_aliases[alias] = sample_name
        return alias

    def _get_submission_alias(self):
        """Format alias using ``self.preprocessed_data_id``"""
        safe_preprocessed_data_id = escape(
            clean_whitespace(str(self.preprocessed_data_id)))
        submission_alias_format = '%s_submission_%s'
        return submission_alias_format % (qiita_config.ebi_organization_prefix,
                                          safe_preprocessed_data_id)

    def _get_run_alias(self, sample_name):
        """Format alias using `sample_name`
        """
        alias = '%s_ppdid_%s:%s' % (
            qiita_config.ebi_organization_prefix,
            escape(clean_whitespace(str(self.preprocessed_data_id))),
            sample_name)
        self._run_aliases[alias] = sample_name
        return alias

    def _get_library_name(self, sample_name):
        """Format alias using `sample_name`
        """
        return escape(clean_whitespace(sample_name))

    def _add_dict_as_tags_and_values(self, parent_node, attribute_element_name,
                                     data_dict):
        """Format key/value data using a common EBI XML motif"""
        for attr, val in sorted(data_dict.items()):
            attribute_element = ET.SubElement(parent_node,
                                              attribute_element_name)
            tag = ET.SubElement(attribute_element, 'TAG')
            tag.text = clean_whitespace(attr)
            value = ET.SubElement(attribute_element, 'VALUE')
            value.text = clean_whitespace(val)

    def _get_pmid_element(self, study_links, pmid):
        study_link = ET.SubElement(study_links, 'STUDY_LINK')
        xref_link = ET.SubElement(study_link,  'XREF_LINK')

        db = ET.SubElement(xref_link, 'DB')
        db.text = 'PUBMED'

        _id = ET.SubElement(xref_link, 'ID')
        _id.text = str(pmid)

    def generate_study_xml(self):
        """Generates the string for study XML file

        Returns
        -------
        ET.Element
            Object with study XML values
        """
        study_set = ET.Element('STUDY_SET', {
            'xmlns:xsi': self.xmlns_xsi,
            'xsi:noNamespaceSchemaLocation': self.xsi_noNSL % "study"})

        study = ET.SubElement(study_set, 'STUDY', {
            'alias': self._get_study_alias(),
            'center_name': qiita_config.ebi_center_name}
        )

        descriptor = ET.SubElement(study, 'DESCRIPTOR')
        study_title = ET.SubElement(descriptor, 'STUDY_TITLE')
        study_title.text = escape(clean_whitespace(self.study_title))

        if self.investigation_type == 'Other':
            ET.SubElement(descriptor, 'STUDY_TYPE', {
                'existing_study_type': 'Other',
                'new_study_type': escape(clean_whitespace(
                    self.new_investigation_type))}
            )
        else:
            ET.SubElement(descriptor, 'STUDY_TYPE', {
                'existing_study_type': escape(clean_whitespace(
                    self.investigation_type))}
            )

        study_abstract = ET.SubElement(descriptor, 'STUDY_ABSTRACT')
        study_abstract.text = clean_whitespace(escape(self.study_abstract))

        # Add pubmed IDs
        if self.pmids:
            study_links = ET.SubElement(study, 'STUDY_LINKS')
            for pmid in self.pmids:
                self._get_pmid_element(study_links, pmid)

        return study_set

    def generate_sample_xml(self, samples=None):
        """Generates the sample XML file

        Parameters
        ----------
        samples : list of str, optional
            The list of samples to be included in the sample xml. If not
            provided or an empty list is provided, all the samples are used

        Returns
        -------
        ET.Element
            Object with sample XML values
        """
        sample_set = ET.Element('SAMPLE_SET', {
            'xmlns:xsi': self.xmlns_xsi,
            "xsi:noNamespaceSchemaLocation": self.xsi_noNSL % "sample"})

        if not samples:
            samples = viewkeys(self.samples)

        for sample_name in sorted(samples):
            sample_info = dict(self.samples[sample_name])
            sample = ET.SubElement(sample_set, 'SAMPLE', {
                'alias': self._get_sample_alias(sample_name),
                'center_name': qiita_config.ebi_center_name}
            )

            sample_title = ET.SubElement(sample, 'TITLE')
            sample_title.text = escape(clean_whitespace(sample_name))

            sample_sample_name = ET.SubElement(sample, 'SAMPLE_NAME')
            taxon_id = ET.SubElement(sample_sample_name, 'TAXON_ID')
            text = sample_info.pop('taxon_id')
            taxon_id.text = escape(clean_whitespace(text))

            scientific_name = ET.SubElement(
                sample_sample_name, 'SCIENTIFIC_NAME')
            text = sample_info.pop('scientific_name')
            scientific_name.text = escape(clean_whitespace(text))

            description = ET.SubElement(sample, 'DESCRIPTION')
            text = sample_info.pop('description')
            description.text = escape(clean_whitespace(text))

            if sample_info:
                sample_attributes = ET.SubElement(sample, 'SAMPLE_ATTRIBUTES')
                self._add_dict_as_tags_and_values(sample_attributes,
                                                  'SAMPLE_ATTRIBUTE',
                                                  sample_info)

        return sample_set

    def _generate_spot_descriptor(self, design, platform):
        """This XML element (and its subelements) must be written for every
        sample, but its generation depends on only study-level information.
        Therefore, we can break it out into its own method.
        """
        # This section applies only to the LS454 platform
        if platform is not 'LS454':
            return

        # There is some hard-coded information in here, but this is what we
        # have always done in the past...
        spot_descriptor = ET.SubElement(design, 'SPOT_DESCRIPTOR')
        ET.SubElement(spot_descriptor, 'SPOT_DECODE_SPEC')
        read_spec = ET.SubElement(spot_descriptor, 'READ_SPEC')

        read_index = ET.SubElement(read_spec, 'READ_INDEX')
        read_index.text = '0'
        read_class = ET.SubElement(read_spec, 'READ_CLASS')
        read_class.text = 'Application Read'
        read_type = ET.SubElement(read_spec, 'READ_TYPE')
        read_type.text = 'Forward'
        base_coord = ET.SubElement(read_spec, 'BASE_COORD')
        base_coord.text = '1'

    def generate_experiment_xml(self, samples=None):
        """Generates the experiment XML file

        Parameters
        ----------
        samples : list of str, optional
            The list of samples to be included in the experiment xml

        Returns
        -------
        ET.Element
            Object with experiment XML values
        """
        study_accession = self.study.ebi_study_accession
        if study_accession:
            study_ref_dict = {'accession': study_accession}
        else:
            study_ref_dict = {'refname': self._get_study_alias()}

        experiment_set = ET.Element('EXPERIMENT_SET', {
            'xmlns:xsi': self.xmlns_xsi,
            "xsi:noNamespaceSchemaLocation": self.xsi_noNSL % "experiment"})

        samples = samples if samples is not None else viewkeys(self.samples)

        for sample_name in sorted(samples):
            experiment_alias = self._get_experiment_alias(sample_name)
            sample_prep = dict(self.samples_prep[sample_name])
            if self._ebi_sample_accessions[sample_name]:
                sample_descriptor_dict = {
                    'accession': self._ebi_sample_accessions[sample_name]}
            else:
                sample_descriptor_dict = {
                    'refname': self._get_sample_alias(sample_name)}

            platform = sample_prep.pop('platform')
            experiment = ET.SubElement(experiment_set, 'EXPERIMENT', {
                'alias': experiment_alias,
                'center_name': qiita_config.ebi_center_name}
            )
            title = ET.SubElement(experiment, 'TITLE')
            title.text = experiment_alias
            ET.SubElement(experiment, 'STUDY_REF', study_ref_dict)

            design = ET.SubElement(experiment, 'DESIGN')
            design_description = ET.SubElement(design,
                                               'DESIGN_DESCRIPTION')
            edd = sample_prep.pop('experiment_design_description')
            design_description.text = escape(clean_whitespace(edd))
            ET.SubElement(design, 'SAMPLE_DESCRIPTOR', sample_descriptor_dict)

            # this is the library contruction section. The only required fields
            # is library_construction_protocol, the other are optional
            library_descriptor = ET.SubElement(design, 'LIBRARY_DESCRIPTOR')
            library_name = ET.SubElement(library_descriptor, 'LIBRARY_NAME')
            library_name.text = self._get_library_name(sample_name)

            # hardcoding some values,
            # see https://github.com/biocore/qiita/issues/1485
            library_source = ET.SubElement(library_descriptor,
                                           "LIBRARY_SOURCE")
            library_source.text = "METAGENOMIC"
            library_selection = ET.SubElement(library_descriptor,
                                              "LIBRARY_SELECTION")
            library_selection.text = "PCR"
            library_layout = ET.SubElement(library_descriptor,
                                           "LIBRARY_LAYOUT")
            ET.SubElement(library_layout, "SINGLE")

            lcp = ET.SubElement(library_descriptor,
                                "LIBRARY_CONSTRUCTION_PROTOCOL")
            lcp.text = escape(clean_whitespace(
                sample_prep.pop('library_construction_protocol')))

            # these are not requiered field but present add them in the right
            # format
            for field in self.experiment_library_fields:
                if field in sample_prep:
                    element = ET.SubElement(library_descriptor, field.upper())
                    element.text = sample_prep.pop(field)

            self._generate_spot_descriptor(design, platform)

            platform_element = ET.SubElement(experiment, 'PLATFORM')
            platform_info = ET.SubElement(platform_element,
                                          platform.upper())
            instrument_model = ET.SubElement(platform_info, 'INSTRUMENT_MODEL')
            instrument_model.text = sample_prep.pop('instrument_model')

            if sample_prep:
                experiment_attributes = ET.SubElement(
                    experiment, 'EXPERIMENT_ATTRIBUTES')
                self._add_dict_as_tags_and_values(experiment_attributes,
                                                  'EXPERIMENT_ATTRIBUTE',
                                                  sample_prep)

        return experiment_set

    def generate_run_xml(self):
        """Generates the run XML file

        Returns
        -------
        ET.Element
            Object with run XML values
        """
        run_set = ET.Element('RUN_SET', {
            'xmlns:xsi': self.xmlns_xsi,
            "xsi:noNamespaceSchemaLocation": self.xsi_noNSL % "run"})
        for sample_name, sample_prep in sorted(viewitems(self.samples_prep)):
            sample_prep = dict(sample_prep)

            if self._ebi_experiment_accessions[sample_name]:
                experiment_ref_dict = {
                    'accession': self._ebi_experiment_accessions[sample_name]}
            else:
                experiment_alias = self._get_experiment_alias(sample_name)
                experiment_ref_dict = {'refname': experiment_alias}

            # We only submit fastq
            file_type = 'fastq'
            file_path = self.sample_demux_fps[sample_name]

            with open(file_path) as fp:
                md5 = safe_md5(fp).hexdigest()

            run = ET.SubElement(run_set, 'RUN', {
                'alias': self._get_run_alias(sample_name),
                'center_name': qiita_config.ebi_center_name}
            )
            ET.SubElement(run, 'EXPERIMENT_REF', experiment_ref_dict)
            data_block = ET.SubElement(run, 'DATA_BLOCK')
            files = ET.SubElement(data_block, 'FILES')
            ET.SubElement(files, 'FILE', {
                'filename': join(self.ebi_dir, basename(file_path)),
                'filetype': file_type,
                'quality_scoring_system': 'phred',
                'checksum_method': 'MD5',
                'checksum': md5}
            )

        return run_set

    def generate_submission_xml(self, submission_date=None):
        """Generates the submission XML file

        Parameters
        ----------
        submission_date : date, optional
            Date when the submission was created, when None date.today() will
            be used.

        Returns
        -------
        ET.Element
            Object with submission XML values

        Notes
        -----
            EBI requieres a date when the submission will be automatically made
            public. This date is generated from the submission date + 365 days.
        """
        submission_set = ET.Element('SUBMISSION_SET', {
            'xmlns:xsi': self.xmlns_xsi,
            "xsi:noNamespaceSchemaLocation": self.xsi_noNSL % "submission"})
        submission = ET.SubElement(submission_set, 'SUBMISSION', {
            'alias': self._get_submission_alias(),
            'center_name': qiita_config.ebi_center_name}
        )

        actions = ET.SubElement(submission, 'ACTIONS')

        if self.study_xml_fp:
            study_action = ET.SubElement(actions, 'ACTION')
            ET.SubElement(study_action, self.action, {
                'schema': 'study',
                'source': basename(self.study_xml_fp)}
            )

        if self.sample_xml_fp:
            sample_action = ET.SubElement(actions, 'ACTION')
            ET.SubElement(sample_action, self.action, {
                'schema': 'sample',
                'source': basename(self.sample_xml_fp)}
            )

        if self.experiment_xml_fp:
            experiment_action = ET.SubElement(actions, 'ACTION')
            ET.SubElement(experiment_action, self.action, {
                'schema': 'experiment',
                'source': basename(self.experiment_xml_fp)}
            )

        run_action = ET.SubElement(actions, 'ACTION')
        ET.SubElement(run_action, self.action, {
            'schema': 'run', 'source': basename(self.run_xml_fp)}
        )

        if submission_date is None:
            submission_date = date.today()
        if self.action == 'ADD':
            hold_action = ET.SubElement(actions, 'ACTION')
            ET.SubElement(hold_action, 'HOLD', {
                'HoldUntilDate': str(submission_date + timedelta(365))}
            )

        return submission_set

    def write_xml_file(self, element, fp):
        """Writes an XML file after calling one of the XML generation
        functions

        Parameters
        ----------
        element : ET.Element
            The Element to be written
        fp : str
            The filepath to which the XML will be written
        """
        create_dir(self.xml_dir)
        ET.ElementTree(element).write(fp, encoding='UTF-8')

    def generate_xml_files(self):
        """Generate all the XML files"""
        get_output_fp = partial(join, self.xml_dir)

        # The study.xml file needs to be generated if and only if the study
        # does NOT have an ebi_study_accession
        if not self.study.ebi_study_accession:
            self.study_xml_fp = get_output_fp('study.xml')
            self.write_xml_file(self.generate_study_xml(), self.study_xml_fp)

        # The sample.xml file needs to be generated if and only if there are
        # samples in the current submission that do NOT have an
        # ebi_sample_accession
        new_samples = {
            sample for sample, accession in viewitems(
                self.sample_template.ebi_sample_accessions)
            if accession is None}
        new_samples = new_samples.intersection(self.samples)
        if new_samples:
            self.sample_xml_fp = get_output_fp('sample.xml')
            self.write_xml_file(self.generate_sample_xml(new_samples),
                                self.sample_xml_fp)

        # The experiment.xml needs to be generated if and only if there are
        # samples in the current submission that do NO have an
        # ebi_experiment_accession
        new_samples = {
            sample for sample, accession in viewitems(
                self.prep_template.ebi_experiment_accessions)
            if accession is None}
        new_samples = new_samples.intersection(self.samples)
        if new_samples:
            self.experiment_xml_fp = get_output_fp('experiment.xml')
            self.write_xml_file(self.generate_experiment_xml(new_samples),
                                self.experiment_xml_fp)

        # Generate the run.xml as it should always be generated
        self.run_xml_fp = get_output_fp('run.xml')
        self.write_xml_file(self.generate_run_xml(), self.run_xml_fp)

        # The submission.xml is always generated
        self.submission_xml_fp = get_output_fp('submission.xml')
        self.write_xml_file(self.generate_submission_xml(),
                            self.submission_xml_fp)

    def generate_curl_command(
            self,
            ebi_seq_xfer_user=qiita_config.ebi_seq_xfer_user,
            ebi_seq_xfer_pass=qiita_config.ebi_seq_xfer_pass,
            ebi_dropbox_url=qiita_config.ebi_dropbox_url):
        """Generates the curl command for submission

        Parameters
        ----------
        ebi_seq_xfer_user : str
            The user to use when submitting to EBI
        ebi_seq_xfer_pass : str
            The user password issued by EBI for REST submissions
        ebi_dropbox_url : str
            The dropbox url

        Returns
        -------
        curl_command
            The curl string to be executed

        Notes
        -----
        - All 5 XML files (study, sample, experiment, run, and submission) must
          be generated before executing this function
        """
        # make sure that the XML files have been generated
        url = '?auth=ENA%20{0}%20{1}'.format(quote(ebi_seq_xfer_user),
                                             quote(ebi_seq_xfer_pass))
        curl_cmd = ['curl -sS -k']
        if self.submission_xml_fp is not None:
            curl_cmd.append(' -F "SUBMISSION=@%s"' % self.submission_xml_fp)
        if self.study_xml_fp is not None:
            curl_cmd.append(' -F "STUDY=@%s"' % self.study_xml_fp)
        if self.sample_xml_fp is not None:
            curl_cmd.append(' -F "SAMPLE=@%s"' % self.sample_xml_fp)
        if self.run_xml_fp is not None:
            curl_cmd.append(' -F "RUN=@%s"' % self.run_xml_fp)
        if self.experiment_xml_fp is not None:
            curl_cmd.append(' -F "EXPERIMENT=@%s"' % self.experiment_xml_fp)
        curl_cmd.append(' "%s"' % join(ebi_dropbox_url, url))

        return ''.join(curl_cmd)

    def generate_send_sequences_cmd(self):
        """Generate the sequences to EBI via ascp command

        Returns
        -------
        ascp_command
            The ascp command to be executed

        Notes
        -----
        - All 5 XML files (study, sample, experiment, run, and submission) must
          be generated before executing this function
        """
        fastqs = [sfp for _, sfp in viewitems(self.sample_demux_fps)]
        # divide all the fastqs in groups of 10
        fastqs_div = [fastqs[i::10] for i in range(10) if fastqs[i::10]]
        ascp_commands = []
        for f in fastqs_div:
            ascp_commands.append('ascp --ignore-host-key -d -QT -k2 '
                                 '{0} {1}@{2}:./{3}/'.format(
                                     ' '.join(f),
                                     qiita_config.ebi_seq_xfer_user,
                                     qiita_config.ebi_seq_xfer_url,
                                     self.ebi_dir))

        return ascp_commands

    def parse_EBI_reply(self, curl_result):
        """Parse and verify reply from EBI after sending XML files

        Parameters
        ----------
        curl_result : str
            The reply sent by EBI after sending XML files

        Returns
        -------
        str
            The study accession number. None in case of failure
        dict of {str: str}
            The sample accession numbers, keyed by sample id. None in case of
            failure
        dict of {str: str}
            The biosample accession numbers, keyed by sample id. None in case
            of failure
        dict of {str: str}
            The experiment accession numbers, keyed by sample id. None in case
            of failure
        dict of {str: str}
            The run accession numbers, keyed by sample id. None in case of
            failure

        Raises
        ------
        EBISubmissionError
            If curl_result is not a valid XML file
            If the ebi subumission has not been successful
            If multiple study tags are found in the curl result
        """
        try:
            root = ET.fromstring(curl_result)
        except ParseError:
            error_msg = ("The curl result from the EBI submission doesn't "
                         "look like an XML file:\n%s" % curl_result)
            le = LogEntry.create('Runtime', error_msg)
            raise EBISubmissionError(
                "The curl result from the EBI submission doesn't look like "
                "an XML file. Contact and admin for more information. "
                "Log id: %d" % le.id)

        success = root.get('success') == 'true'
        if not success:
            raise EBISubmissionError("The EBI submission failed:\n%s"
                                     % curl_result)

        study_elem = root.findall("STUDY")
        if study_elem:
            if len(study_elem) > 1:
                raise EBISubmissionError(
                    "Multiple study tags found in EBI reply: %d"
                    % len(study_elem))
            study_elem = study_elem[0]
            study_accession = study_elem.get('accession')
        else:
            study_accession = None

        sample_accessions = {}
        biosample_accessions = {}
        for elem in root.iter("SAMPLE"):
            alias = elem.get('alias')
            sample_id = self._sample_aliases[alias]
            sample_accessions[sample_id] = elem.get('accession')
            ext_id = elem.find('EXT_ID')
            biosample_accessions[sample_id] = ext_id.get('accession')

        def data_retriever(key, trans_dict):
            res = {}
            for elem in root.iter(key):
                alias = elem.get('alias')
                res[trans_dict[alias]] = elem.get('accession')
            return res
        experiment_accessions = data_retriever("EXPERIMENT",
                                               self._experiment_aliases)
        run_accessions = data_retriever("RUN", self._run_aliases)

        return (study_accession, sample_accessions, biosample_accessions,
                experiment_accessions, run_accessions)

    def generate_demultiplexed_fastq(self, rewrite_fastq=False, mtime=None):
        """Generates demultiplexed fastq

        Parameters
        ----------
        rewrite_fastq : bool, optional
            If true, it forces the rewrite of the fastq files
        mtime : float, optional
            The time to use when creating the gz files. If None, the current
            time will be used by gzip.GzipFile. This is useful for testing.

        Returns
        -------
        demux_samples
            List of successful demultiplexed samples

        Notes
        -----
        - As a performace feature, this method will check if self.full_ebi_dir
        already exists and, if it does, the script will assume that in a
        previous execution this step was performed correctly and will simply
        read the file names from self.full_ebi_dir
        - When the object is created (init), samples, samples_prep and
        sample_demux_fps hold values for all available samples in the database.
        Here some of those values will be deleted (del's, within the loops) for
        those cases where the fastq.gz files weren't written or exist. This is
        an indication that they had no sequences and this kind of files are not
        accepted in EBI

        Raises
        ------
        EBISubmissionError
            - The demux file couldn't be read
            - All samples are removed
        """
        ppd = self.preprocessed_data

        dir_not_exists = not isdir(self.full_ebi_dir)
        if dir_not_exists or rewrite_fastq:
            makedirs(self.full_ebi_dir)

            demux = [path for _, path, ftype in ppd.get_filepaths()
                     if ftype == 'preprocessed_demux'][0]

            demux_samples = set()
            with open_file(demux) as demux_fh:
                if not isinstance(demux_fh, File):
                    error_msg = "'%s' doesn't look like a demux file" % demux
                    LogEntry.create('Runtime', error_msg)
                    raise EBISubmissionError(error_msg)
                for s, i in to_per_sample_ascii(demux_fh,
                                                self.prep_template.keys()):
                    sample_fp = self.sample_demux_fps[s]
                    wrote_sequences = False
                    with GzipFile(sample_fp, mode='w', mtime=mtime) as fh:
                        for record in i:
                            fh.write(record)
                            wrote_sequences = True

                    if wrote_sequences:
                        demux_samples.add(s)
                    else:
                        del(self.samples[s])
                        del(self.samples_prep[s])
                        del(self.sample_demux_fps[s])
                        remove(sample_fp)
        else:
            demux_samples = set()
            extension = '.fastq.gz'
            extension_len = len(extension)
            for f in listdir(self.full_ebi_dir):
                fpath = join(self.full_ebi_dir, f)
                if isfile(fpath) and f.endswith(extension):
                    demux_samples.add(f[:-extension_len])

            missing_samples = set(self.samples.keys()).difference(
                set(demux_samples))
            for ms in missing_samples:
                del(self.samples[ms])
                del(self.samples_prep[ms])
                del(self.sample_demux_fps[ms])

        if not demux_samples:
            error_msg = ("All samples were removed from the submission "
                         "because the demux file is empty or the sample names "
                         "do not match.")
            LogEntry.create('Runtime', error_msg)
            raise EBISubmissionError(error_msg)
        return demux_samples
