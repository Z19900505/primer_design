# %load /home/hanliu/pkg/primer_design/primer.py
"""
Author Hanqing Liu

This file contain functions for using Primer3

Input:
- A bed file indicates targeted region/sites
- Genome fasta file corresponding to the bed file
- Config file for primer 3
"""

import pandas as pd
import pathlib
import subprocess
import collections
from Bio.Blast.Applications import NcbiblastnCommandline
from Bio import SearchIO


def _read_bed(file_path):
    """
    Read a bed file, 4 columns: ['seq_name', 'start', 'end', 'region_id']
    """
    bed_df = pd.read_table(file_path,
                           header=None, comment='#',
                           names=['seq_name', 'start', 'end', 'region_id'])
    return bed_df.set_index('region_id')


def _read_fasta_fai(fai_path):
    """read samtools faidx for the genome fasta"""
    fai_df = pd.read_table(fai_path,
                           index_col=0, header=None,
                           names=['seq_name', 'length', 'start_at',
                                  'line_seq_length', 'line_total_length'])
    return fai_df


def _query_genome(fasta_path, fai_df,
                  seq_name, region_start, region_end,
                  left_expand, right_expand, primer_name):
    """
    Query the DNA sequence for a region from a fasta file

    Parameters
    ----------
    fasta_path
        path of fasta file
    fai_df
        fai dataframe corresponding to fasta
    seq_name
        name of the sequence in fasta, equal to chromosome name if using genome
    region_start
        0 based start position
    region_end
        0 based end position
    left_expand
        length of left expanding when query sequence
    right_expand
        length of right expanding when query sequence
    primer_name
        name of the primer

    Returns
    -------
    query_result
        A series contain primer name, template sequence and target region
        (coords based on template sequence not based on original fasta)

    """

    # check fai, get position
    if seq_name not in fai_df.index:
        raise KeyError(f'{seq_name} not in the faidx file of genome fasta {fasta_path}')
    if region_end < region_start:
        raise ValueError(
            f'Region end {region_end} < Region Start {region_start} at primer {primer_name}, check your input')

    # calculate length
    seq_start_pos = fai_df.loc[seq_name, 'start_at']
    line_seq_length = fai_df.loc[seq_name, 'line_seq_length']
    line_total_length = fai_df.loc[seq_name, 'line_total_length']
    real_region_start_post = max(0, region_start - left_expand)
    chrom_query_start = seq_start_pos + real_region_start_post // line_seq_length * line_total_length + \
                        real_region_start_post % line_seq_length

    # in case the region start is close to ref sequence start
    if real_region_start_post == 0:
        real_left_expand = region_start
    else:
        real_left_expand = left_expand
    chrom_query_length = real_left_expand + (region_end - region_start) + right_expand

    # check some error
    if chrom_query_length > 999999:
        raise ValueError(f'At primer {primer_name}, do you really want to design primer with lenght > 999999?')
    if fasta_path.endswith('.gz'):
        raise NotImplementedError(
            'Genome fasta is gziped, query sequence from gzip file could be super slow, '
            'unzip it should be much faster.')

    # get sequence
    with open(fasta_path) as f:
        f.seek(chrom_query_start)
        sequence_context = ''
        for line in f:
            if line[0] == '>':  # read to next seq
                break
            elif len(sequence_context) >= chrom_query_length:  # read enough length
                break
            else:
                sequence_context += line.strip()
        query_sequence = sequence_context[:min(chrom_query_length, len(sequence_context))]
    if len(sequence_context) >= chrom_query_length:
        real_right_expand = right_expand
    else:
        real_right_expand = len(sequence_context) - real_left_expand - (region_end - region_start)
    if real_right_expand < 0:
        raise ValueError(f'At primer {primer_name}, Region end is outside reference sequence.')

    query_result = pd.Series({  # following primer3 tag names
        'SEQUENCE_NAME': primer_name,
        'SEQUENCE_TEMPLATE': query_sequence.upper(),
        'SEQUENCE_TARGET': f'{real_left_expand},{region_end - region_start}'
    })
    return query_result


def _run_primer3(primer_template_df, setting_dict):
    """
    Primer 3 single runner
    """
    results = []
    for primer_name, record in primer_template_df.iterrows():
        # modify setting_dict, prepare primer specific input
        row_input_dict = setting_dict.copy()
        for k, v in record.iteritems():
            if k != '':
                row_input_dict[k] = v
        row_input_dict['SEQUENCE_NAME'] = primer_name
        primer3_input = ''

        for k, v in row_input_dict.items():
            if k != '':
                primer3_input += f'{k}={v}\n'
        primer3_input += '=\n'
        # run
        result = subprocess.run(args=['primer3_core'],
                                input=primer3_input,
                                stdout=subprocess.PIPE,
                                encoding='utf8',
                                check=False)
        results.append(result)

    primer_stats = []
    primers = []
    for primer3_return in results:
        if primer3_return.returncode != 0:
            print(primer3_return)
            return
        primer3_out = primer3_return.stdout
        primer_stat_df, primer_df = _parse_primer3_result(primer3_out)
        primer_stats.append(primer_stat_df)
        primers.append(primer_df)
    total_primer_stat_df = pd.concat(primer_stats, sort=True)
    total_primer_df = pd.concat(primers, sort=True)
    return primer_template_df, total_primer_stat_df, total_primer_df


def _parse_primer3_result(primer3_out):
    # split stat part and primer part
    stat_dict = {}
    primer_dict = {}
    primer_info_start = False
    for line in primer3_out.split('\n'):
        ll = line.strip().split('=')
        if len(ll) != 2:
            continue
        if primer_info_start:
            primer_dict[ll[0]] = ll[1]
        else:
            if ll[0].startswith('PRIMER_PAIR_0'):
                primer_info_start = True
                primer_dict[ll[0]] = ll[1]
            else:
                stat_dict[ll[0]] = ll[1]

    # get primer stat df
    primer_stat_records = []
    primer_stat_dict = {}
    for condition in stat_dict['PRIMER_LEFT_EXPLAIN'].split(','):
        *condition_name, value = condition.strip().split(' ')
        condition_name = '_'.join(condition_name)
        primer_stat_dict[condition_name] = value
    primer_stat_dict['primer_type'] = 'left'
    primer_stat_records.append(pd.Series(primer_stat_dict))
    primer_stat_dict = {}
    for condition in stat_dict['PRIMER_RIGHT_EXPLAIN'].split(','):
        *condition_name, value = condition.strip().split(' ')
        condition_name = '_'.join(condition_name)
        primer_stat_dict[condition_name] = value
    primer_stat_dict['primer_type'] = 'right'
    primer_stat_records.append(pd.Series(primer_stat_dict))
    primer_stat_df = pd.DataFrame(primer_stat_records)
    primer_stat_df['PRIMER_NAME'] = stat_dict['SEQUENCE_NAME']

    # get primer df
    primer_record_dict = collections.defaultdict(dict)
    for k, v in primer_dict.items():
        kl = k.split('_')
        if len(kl) < 4:
            continue
        primer_id = f"{stat_dict['SEQUENCE_NAME']}_{kl[2]}"
        item_name = '_'.join(kl[:2] + kl[3:])
        primer_record_dict[primer_id][item_name] = v
    primer_df = pd.DataFrame(primer_record_dict).T
    primer_df['PRIMER_NAME'] = stat_dict['SEQUENCE_NAME']
    return primer_stat_df, primer_df


def _judge_potential_products(total_primer_df, products_cutoff=1, ratio=2):
    valid_dict = {}
    for primer, row in total_primer_df.iterrows():
        if row['POTENTIAL_PRODUCT_LENGTHS'] == '':
            valid_dict[primer] = False
        else:
            potential_products = map(int, row['POTENTIAL_PRODUCT_LENGTHS'].split('|'))
            max_length = min(int(row['PRIMER_PAIR_PRODUCT_SIZE']) * ratio, 20000)
            valid_count = sum([i < max_length for i in potential_products])
            valid_dict[primer] = valid_count <= products_cutoff
    products_judge = pd.Series(valid_dict)
    return products_judge


def _dump_primer_fasta(total_primer_df, out_path):
    primer_fasta = ''
    for primer_id, sequence in total_primer_df['PRIMER_RIGHT_SEQUENCE'].iteritems():
        fasta_record = f'>{primer_id}_r\n{sequence}\n'
        primer_fasta += fasta_record
    for primer_id, sequence in total_primer_df['PRIMER_LEFT_SEQUENCE'].iteritems():
        fasta_record = f'>{primer_id}_l\n{sequence}\n'
        primer_fasta += fasta_record
    with open(out_path, 'w') as f:
        f.write(primer_fasta)


def _blast_primer(primer_fasta_path,
                  db_path,
                  evalue_cutoff=1000,
                  min_total_mismatch_portion=0.2,
                  min_total_mismatch=6,
                  min_prime_3_mismatch=2,
                  prime_3_length=5,
                  alt_pos_cutoff=2000,
                  max_product_size=5000,
                  word_size=7):
    """
    Take a fasta file as input, query genome db and count qualified hits

    Parameters
    ----------
    primer_fasta_path
    db_path
    evalue_cutoff
    min_total_mismatch_portion
    min_total_mismatch
    min_prime_3_mismatch
    prime_3_length
    alt_pos_cutoff
    max_product_size
    word_size

    Returns
    -------

    """
    # run blastn for all primers
    temp_dir = pathlib.Path(primer_fasta_path).parent

    blast_cline = NcbiblastnCommandline(query=str(primer_fasta_path),
                                        db=db_path,
                                        evalue=evalue_cutoff,
                                        outfmt=5,
                                        word_size=word_size,
                                        out=str(temp_dir / (primer_fasta_path.stem +
                                                            "_blast_result.xml")),
                                        task='blastn')
    blast_cline()

    # parse blast result
    blast_results = SearchIO.parse(temp_dir / (primer_fasta_path.stem +
                                               "_blast_result.xml"), "blast-xml")
    primer_hit_dict = {}
    for blast_result in blast_results:
        primer_length = blast_result.seq_len
        primer_total_mismatch = max(min_total_mismatch, min_total_mismatch_portion * primer_length)
        alternate_hsps = []
        for hit in blast_result:
            for hsp in hit.hsps:
                prime_5_unmatch = [' ' for _ in range(hsp.query_range[0])]
                prime_3_unmatch = [' ' for _ in range(primer_length - hsp.query_range[1])]
                align_anno = prime_5_unmatch + list(hsp.aln_annotation['similarity']) + prime_3_unmatch
                align_anno = ''.join(align_anno)

                total_mismatch = primer_length - align_anno.count('|')
                if total_mismatch > primer_total_mismatch:
                    continue

                prime_3_mismatch = prime_3_length - align_anno[-prime_3_length:].count('|')
                if prime_3_mismatch > min_prime_3_mismatch:
                    continue
                alternate_hsps.append(hsp)
        *primer_name, direction = blast_result.id.split('_')
        primer_name = '_'.join(primer_name)
        append_pos = 0 if direction == 'l' else 1
        if primer_name not in primer_hit_dict:
            primer_hit_dict[primer_name] = [[], []]
        primer_hit_dict[primer_name][append_pos] += alternate_hsps
    primer_hit_records = {}
    for primer, (left_hits, right_hits) in primer_hit_dict.items():
        if (len(left_hits) > alt_pos_cutoff) or (len(right_hits) > alt_pos_cutoff):
            continue
        else:
            valid_product_lengths = []
            positive_strand_hit = [hit for hit in left_hits if hit.hit_strand == 1] + \
                                  [hit for hit in right_hits if hit.hit_strand == 1]
            negative_strand_hit = [hit for hit in left_hits if hit.hit_strand == -1] + \
                                  [hit for hit in right_hits if hit.hit_strand == -1]

            for positive_hit in positive_strand_hit:
                for negative_hit in negative_strand_hit:
                    # hit not in same chrom
                    if positive_hit.hit_id != negative_hit.hit_id:
                        continue
                    else:
                        product_size = abs(positive_hit.hit_range[0] - negative_hit.hit_range[1])
                        # left right too far away
                        if product_size > max_product_size:
                            continue

                    valid_product_lengths.append(str(product_size))
            primer_hit_records[primer] = {
                'LEFT_GENOME_HITS': len(left_hits),
                'RIGHT_GENOME_HITS': len(right_hits),
                'POTENTIAL_PRODUCTS': len(valid_product_lengths),
                'POTENTIAL_PRODUCT_LENGTHS': '|'.join(valid_product_lengths),
            }
    primer_hit_df = pd.DataFrame(primer_hit_records).T
    return primer_hit_df


def primer_blast(bed_path, target_fasta_path, primer3_setting_path, blast_db_path,
                 left_expand=None, right_expand=None, both_expand=100,
                 max_length=99999, drop_too_long=False, blast_kws=None,
                 **config_kws):
    """
    Main function, mimic NCBI primer-blast, take a bed file as input,
    query genome and get DNA sequence for regions listed in the bed,
    use primer3 to design primer and use blast to check primer specificity.

    Parameters
    ----------
    bed_path
    target_fasta_path
    primer3_setting_path
    blast_db_path
    left_expand
    right_expand
    both_expand
    max_length
    drop_too_long
    blast_kws
    config_kws

    Returns
    -------

    """
    out_dir = pathlib.Path(bed_path).parent

    # parse config
    setting_dict = {}
    with open(primer3_setting_path) as f:
        for line in f:
            ll = line.strip().split('=')
            if (len(ll) != 2) or (ll[0] == 'P3_FILE_TYPE'):
                continue
            setting_dict[ll[0]] = ll[1]
    for k, v in config_kws.items():
        setting_dict[k.upper()] = v

    # all the number within primer3 config are not carefully checked, remain this to primer3.
    if left_expand is None:
        left_expand = both_expand
    if right_expand is None:
        right_expand = both_expand
    if left_expand is None or right_expand is None:
        raise ValueError('Specify left_expand & right_expand, or both_expand')

    bed_df = _read_bed(bed_path)
    if not pathlib.Path(target_fasta_path + '.fai').exists():
        raise FileNotFoundError(
            f'{target_fasta_path} do not have .fai index, use samtools faidx to index the file first')
    fai_df = _read_fasta_fai(target_fasta_path + '.fai')

    primer_records = []
    for primer_name, (seq_name, start, end) in bed_df.iterrows():
        region_length = start - end
        total_length = left_expand + region_length + right_expand
        if region_length > max_length:
            print(f'{primer_name} is dropped due to exceed {max_length} bp')
            continue
        elif total_length > max_length:
            if drop_too_long:
                print(f'{primer_name} is dropped due to exceed {max_length} bp (including expansion)')
                continue
            print(f'{primer_name} expansion is shorten due to exceed {max_length} bp')
            extra_length = max_length - region_length
            left_portion = left_expand / (left_expand + right_expand)
            left_expand = int(extra_length * left_portion)
            right_expand = extra_length - left_expand

        primer_series = _query_genome(fasta_path=target_fasta_path,
                                      fai_df=fai_df,
                                      seq_name=seq_name,
                                      region_start=start,
                                      region_end=end,
                                      left_expand=left_expand,
                                      right_expand=right_expand,
                                      primer_name=primer_name)
        primer_records.append(primer_series)
    primer_template_df = pd.DataFrame(primer_records).set_index('SEQUENCE_NAME')
    primer_template_df, total_primer_stat_df, total_primer_df = _run_primer3(primer_template_df,
                                                                             setting_dict)
    # in case no primer can be designed, total_primer_df is empty:
    if total_primer_df.shape[0] == 0:
        return primer_template_df, total_primer_stat_df, None
    _dump_primer_fasta(total_primer_df, out_dir / (pathlib.Path(bed_path).stem + '_primer.fa'))

    if blast_kws is None:
        blast_kws = {}
    primer_hit_df = _blast_primer(primer_fasta_path=out_dir / (pathlib.Path(bed_path).stem + '_primer.fa'),
                                  db_path=blast_db_path,
                                  **blast_kws)
    total_primer_df = pd.concat([total_primer_df, primer_hit_df], axis=1, sort=True)
    total_primer_df = total_primer_df.reindex(primer_hit_df.index)

    potential_product_judge = _judge_potential_products(total_primer_df, products_cutoff=1, ratio=3)
    filtered_primer_df = total_primer_df.loc[potential_product_judge]
    selected_primer = filtered_primer_df[['PRIMER_RIGHT_PENALTY', 'PRIMER_LEFT_PENALTY']] \
        .astype(float) \
        .sum(axis=1) \
        .groupby(filtered_primer_df.index.map(lambda i: i.split('_')[0])) \
        .apply(lambda sub_df: sub_df.sort_values().index[0]).tolist()

    total_primer_df['Selected'] = total_primer_df.index.map(
        lambda i: True if i in set(selected_primer) else False)

    primer_template_df.to_csv(out_dir / (pathlib.Path(bed_path).stem + '_primer_template.tsv.gz'),
                              sep='\t', compression='gzip')
    total_primer_stat_df.to_csv(out_dir / (pathlib.Path(bed_path).stem + '_primer3_stat.tsv.gz'),
                                sep='\t', compression='gzip')
    total_primer_df.to_csv(out_dir / (pathlib.Path(bed_path).stem + '_primer.tsv.gz'),
                           sep='\t', compression='gzip')
    subprocess.run(['rm', str(out_dir / (pathlib.Path(bed_path).stem + '_primer_blast_result.xml'))])
    return primer_template_df, total_primer_stat_df, total_primer_df
