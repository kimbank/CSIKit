import os
import struct
from time import time

import numpy as np

from math import floor

from CSIKit.csi import CSIData
from CSIKit.csi.frames import IWLCSIFrame
from CSIKit.reader import Reader

from CSIKit.util.errors import print_length_error
from CSIKit.util.matlab import db, dbinv

SIZE_STRUCT = struct.Struct(">H").unpack
CODE_STRUCT = struct.Struct("B").unpack

HEADER_STRUCT = struct.Struct("<LHHBBBBBbBBHH").unpack
VALID_BEAMFORMING_MEASUREMENT = 187

class IWLBeamformReader(Reader):
    """
        This class handles parsing for CSI data from both batched files and realtime CSI packets from IWL5300 hardware.
        It is optimised for speed, with minor sanity checking throughout.
        On a modern system, a frame can usually be processed in around 9e-4 seconds.

        The testing options allow for mat files to be generated, whose integrity can be verified with the matlab/intelcompare.m script.
    """

    def __init__(self):
        """
            Constructor of IWLBeamformReader class
            
        """
        pass

    @staticmethod
    def can_read(path):
        if os.path.exists(path):
            _, extension = os.path.splitext(path)
            if extension == ".dat":
                return True
            else:
                return False
        else:
            raise Exception("File not found: {}".format(path))

    @staticmethod
    def read_bfee(header, data, perm, scaled, i=0, filename=""):
        """
            This function parses a CSI payload using its preconstructed header and returns a complete block object containing header information and a CSI matrix.

            Parameters:
                header (list): A list containing fields for a given CSI frame header.
                data (bytes): A bytes object containing the payload for CSI data.
                perm (list): Permutation parameters for CSI matrix.
                i (int): Index of the given frame (optional, useful for large files).
            
            Returns:
                csi_block (dict): An object containing frame header information and a CSI matrix.
        """

        n_rx = header[3]
        n_tx = header[4]
        expected_length = header[11]

        #Flag invalid payloads so we don't error out trying to parse them into matrices.
        actual_length = len(data)
        if expected_length != actual_length:
            # return print_length_error(expected_length, actual_length, i, filename)
            return None

        csi = np.empty((30, n_rx, n_tx), dtype=np.complex)

        index = 0
        for i in range(30):
            index += 3
            remainder = index % 8
            for j in range(n_rx):
                for k in range(n_tx):
                    ind8 = floor(index/8)

                    if (ind8+2 >= len(data)):
                        break

                    real = (data[ind8] >> remainder) | (data[1+ind8] << (8-remainder))
                    imag = (data[1+ind8] >> remainder) | (data[2+ind8] << (8-remainder))

                    real = np.int8(real)
                    imag = np.int8(imag)

                    complex_no = real + imag * 1j

                    try:
                        csi[i][perm[j]][k] = complex_no
                    except IndexError as _:
                        #Minor backup in instances where severely invalid permutation parameters are generated.
                        csi[i][j][k] = complex_no

                    index += 16

        if scaled:
            scaled_csi = IWLBeamformReader.scale_csi_entry(csi, header)
            return scaled_csi
        else:
            return csi

    @staticmethod
    def read_bf_entry(data, scaled=False):
        """
            This function parses a realtime CSI payload not associated with a file (for example: those extracted via netlink).

            Parameters:
                data (bytes): The total bytes returned by the kernel for a CSI frame.

            Returns:
                csi_block (dict): Individual parsed CSI block.
        """

        csi_header = struct.unpack("<LHHBBBBBbBBHH", data[4:25])
        all_data = [x[0] for x in struct.Struct(">B").iter_unpack(data[25:])]

        n_rx = csi_header[3]
        antenna_sel = csi_header[10]

        #If less than 3 Rx antennas are detected, default permutation should be used.
        #Otherwise invalid indices will likely be raised.
        perm = [0, 1, 2]
        if sum(perm) == n_rx:
            perm[0] = ((antenna_sel) & 0x3)
            perm[1] = ((antenna_sel >> 2) & 0x3)
            perm[2] = ((antenna_sel >> 4) & 0x3)

        csi_block = IWLBeamformReader.read_bfee(csi_header, all_data, perm, scaled)

        return csi_block

    def read_file(self, path, scaled=False):
        """
            This function parses .dat files generated by log_to_file.

            Parameters:
                file (filereader): File reader object returned from open().

            Returns:
                total_csi (list): All valid CSI blocks contained within the given file.
        """
        self.filename = os.path.basename(path)

        ret_data = CSIData(self.filename, "Intel IWL5300")
        ret_data.bandwidth = 20

        if not os.path.exists(path):
            raise Exception("File not found: {}".format(path))

        data = open(path, "rb").read()

        length = len(data)

        cursor = 0

        initial_timestamp = 0

        while (length - cursor) > 100:
            size = SIZE_STRUCT(data[cursor:cursor+2])[0]
            code = CODE_STRUCT(data[cursor+2:cursor+3])[0]
            
            cursor += 3

            if code == VALID_BEAMFORMING_MEASUREMENT:
                all_block = data[cursor:cursor+size-1]

                header_block = HEADER_STRUCT(all_block[:20])
                data_block = all_block[20:]

                #Going to leave permutation params out of the data for now.
                #At some point, this needs to end up in the header_block.
                #I'd prefer that to passing it as a parameter in the constructor.
                #But since it's derived, it can't be in the HEADER_STRUCT. Lame.

                n_rx = header_block[3]
                antenna_sel = header_block[10]

                #If less than 3 Rx antennas are detected, default permutation should be used.
                #Otherwise invalid indices will likely be raised.
                perm = [0, 1, 2]
                if sum(perm) == n_rx:
                    perm[0] = ((antenna_sel) & 0x3)
                    perm[1] = ((antenna_sel >> 2) & 0x3)
                    perm[2] = ((antenna_sel >> 4) & 0x3)

                csi_matrix = IWLBeamformReader.read_bfee(header_block, data_block, perm, scaled, ret_data.expected_frames)
                if csi_matrix is not None:
                    frame = IWLCSIFrame(header_block, csi_matrix)
                    ret_data.push_frame(frame)

                    timestamp_low = header_block[0]*10e-7

                    if initial_timestamp == 0:
                        initial_timestamp = timestamp_low

                    ret_data.timestamps.append(timestamp_low - initial_timestamp)

            else:
                print("Invalid code for beamforming measurement at {}.".format(hex(cursor)))

            ret_data.expected_frames += 1
            cursor += size-1

        return ret_data

    @staticmethod
    def get_total_rss(rssi_a, rssi_b, rssi_c, agc):
        # Calculates the Received Signal Strength (RSS) in dBm
        # Careful here: rssis could be zero

        rssi_mag = 0
        if rssi_a != 0:
            rssi_mag = rssi_mag + dbinv(rssi_a)
        if rssi_b != 0:
            rssi_mag = rssi_mag + dbinv(rssi_b)
        if rssi_c != 0:
            rssi_mag = rssi_mag + dbinv(rssi_c)

        #Interpreting RSS magnitude as power for RSS/dBm conversion.
        #This is consistent with Linux 802.11n CSI Tool's MATLAB implementation.
        #As seen in get_total_rss.m.
        return db(rssi_mag, "pow") - 44 - agc

    @staticmethod
    def scale_csi_entry(csi, header):
        """
            This function performs scaling on the retrieved CSI data to account for automatic gain control and other factors.
            Code within this section is largely based on the Linux 802.11n CSI Tool's MATLAB implementation (get_scaled_csi.m).

            Parameters:
                frame {dict} -- CSI frame object for which CSI is to be scaled.
        """

        n_rx = header[3]
        n_tx = header[4]

        rssi_a = header[5]
        rssi_b = header[6]
        rssi_c = header[7]

        noise = header[8]
        agc = header[9]
        
        #Calculate the scale factor between normalized CSI and RSSI (mW).
        csi_sq = np.multiply(csi, np.conj(csi))
        csi_pwr = np.sum(csi_sq)
        csi_pwr = np.real(csi_pwr)

        rssi_pwr_db = IWLBeamformReader.get_total_rss(rssi_a, rssi_b, rssi_c, agc)
        rssi_pwr = dbinv(rssi_pwr_db)
        #Scale CSI -> Signal power : rssi_pwr / (mean of csi_pwr)
        scale = rssi_pwr / (csi_pwr / 30)

        #Thermal noise may be undefined if the trace was captured in monitor mode.
        #If so, set it to 92.
        noise_db = noise
        if (noise == -127):
            noise_db = -92

        noise_db = np.float(noise_db)
        thermal_noise_pwr = dbinv(noise_db)

        #Quantization error: the coefficients in the matrices are 8-bit signed numbers,
        #max 127/-128 to min 0/1. Given that Intel only uses a 6-bit ADC, I expect every
        #entry to be off by about +/- 1 (total across real and complex parts) per entry.

        #The total power is then 1^2 = 1 per entry, and there are Nrx*Ntx entries per
        #carrier. We only want one carrier's worth of error, since we only computed one
        #carrier's worth of signal above.
        quant_error_pwr = scale * (n_rx * n_tx)

        #Noise and error power.
        total_noise_pwr = thermal_noise_pwr + quant_error_pwr

        # ret now has units of sqrt(SNR) just like H in textbooks.
        ret = csi * np.sqrt(scale / total_noise_pwr)
        if n_tx == 2:
            ret = ret * np.sqrt(2)
        elif n_tx == 3:
            #Note: this should be sqrt(3)~ 4.77dB. But 4.5dB is how
            #Intel and other makers approximate a factor of 3.
            #You may need to change this if your card does the right thing.
            ret = ret * np.sqrt(dbinv(4.5))

        return ret