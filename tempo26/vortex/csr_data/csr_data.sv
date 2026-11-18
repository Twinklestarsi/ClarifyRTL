// Copyright (c) 2019-2023 Vortex project contributors
// Standalone CSR data path adapted for an isolated Spec2RTL case.
// This implementation keeps the stateful machine-CSR behavior, counter
// slicing, custom GPU state, per-warp state, access validation, and a
// registered read-response path.  It has no package or include dependency.
`timescale 1ns / 1ps
`default_nettype none

module VX_csr_data #(
    parameter integer XLEN         = 32,
    parameter integer ADDR_WIDTH   = 12,
    parameter integer NUM_WARPS    = 4,
    parameter integer NUM_THREADS  = 8,
    parameter integer CORE_ID      = 0,
    parameter integer STARTUP_ARG  = 0,
    parameter integer VENDOR_ID    = 0,
    parameter integer ARCH_ID      = 1,
    parameter integer IMPL_ID      = 1,
    parameter integer NUM_CORES    = 1,
    parameter integer NUM_CLUSTERS = 1,
    parameter integer LMEM_BASE    = 0,
    parameter integer TIMING_BUG   = 0,
    parameter integer WID_WIDTH    = $clog2(NUM_WARPS)
) (
    input  wire                   clk,
    input  wire                   reset,
    input  wire [           63:0] cycles,
    input  wire [           63:0] instret,
    input  wire [  NUM_WARPS-1:0] active_warps,
    input  wire [NUM_THREADS-1:0] thread_mask,
    input  wire                   read_enable,
    input  wire [ ADDR_WIDTH-1:0] read_addr,
    input  wire [  WID_WIDTH-1:0] read_wid,
    output wire                   read_valid,
    output wire [       XLEN-1:0] read_data,
    output wire                   read_error,
    input  wire                   write_enable,
    input  wire [ ADDR_WIDTH-1:0] write_addr,
    input  wire [       XLEN-1:0] write_data,
    output wire                   write_ready
);
  // Standard read-only machine identification CSRs.
  localparam logic [ADDR_WIDTH-1:0] CSR_MVENDORID = 12'hF11;
  localparam logic [ADDR_WIDTH-1:0] CSR_MARCHID = 12'hF12;
  localparam logic [ADDR_WIDTH-1:0] CSR_MIMPID = 12'hF13;
  localparam logic [ADDR_WIDTH-1:0] CSR_MHARTID = 12'hF14;
  localparam logic [ADDR_WIDTH-1:0] CSR_MCONFIGPTR = 12'hF15;

  // Standard machine control and status CSRs.
  localparam logic [ADDR_WIDTH-1:0] CSR_MSTATUS = 12'h300;
  localparam logic [ADDR_WIDTH-1:0] CSR_MISA = 12'h301;
  localparam logic [ADDR_WIDTH-1:0] CSR_MEDELEG = 12'h302;
  localparam logic [ADDR_WIDTH-1:0] CSR_MIDELEG = 12'h303;
  localparam logic [ADDR_WIDTH-1:0] CSR_MIE = 12'h304;
  localparam logic [ADDR_WIDTH-1:0] CSR_MTVEC = 12'h305;
  localparam logic [ADDR_WIDTH-1:0] CSR_MCOUNTEREN = 12'h306;
  localparam logic [ADDR_WIDTH-1:0] CSR_MSCRATCH = 12'h340;
  localparam logic [ADDR_WIDTH-1:0] CSR_MEPC = 12'h341;
  localparam logic [ADDR_WIDTH-1:0] CSR_MCAUSE = 12'h342;
  localparam logic [ADDR_WIDTH-1:0] CSR_MTVAL = 12'h343;
  localparam logic [ADDR_WIDTH-1:0] CSR_MIP = 12'h344;
  localparam logic [ADDR_WIDTH-1:0] CSR_SATP = 12'h180;

  // Counter addresses use the RISC-V low/high split for XLEN=32.
  localparam logic [ADDR_WIDTH-1:0] CSR_MCYCLE = 12'hB00;
  localparam logic [ADDR_WIDTH-1:0] CSR_MINSTRET = 12'hB02;
  localparam logic [ADDR_WIDTH-1:0] CSR_MCYCLEH = 12'hB80;
  localparam logic [ADDR_WIDTH-1:0] CSR_MINSTRETH = 12'hB82;

  // Vortex custom execution-state CSRs.
  localparam logic [ADDR_WIDTH-1:0] CSR_WARP_ID = 12'hCC0;
  localparam logic [ADDR_WIDTH-1:0] CSR_CORE_ID = 12'hCC1;
  localparam logic [ADDR_WIDTH-1:0] CSR_ACTIVE_THREADS = 12'hCC2;
  localparam logic [ADDR_WIDTH-1:0] CSR_ACTIVE_WARPS = 12'hCC3;
  localparam logic [ADDR_WIDTH-1:0] CSR_NUM_THREADS = 12'hCC4;
  localparam logic [ADDR_WIDTH-1:0] CSR_NUM_WARPS = 12'hCC5;
  localparam logic [ADDR_WIDTH-1:0] CSR_NUM_CORES = 12'hCC6;
  localparam logic [ADDR_WIDTH-1:0] CSR_LOCAL_MEM_BASE = 12'hCC7;
  localparam logic [ADDR_WIDTH-1:0] CSR_READ_COUNT = 12'hCC8;
  localparam logic [ADDR_WIDTH-1:0] CSR_WRITE_COUNT = 12'hCC9;
  localparam logic [ADDR_WIDTH-1:0] CSR_ERROR_COUNT = 12'hCCA;
  localparam logic [ADDR_WIDTH-1:0] CSR_WARP_SCRATCH = 12'hCCB;

  // Stateful writable machine registers.
  logic [XLEN-1:0] mstatus;
  logic [XLEN-1:0] mie;
  logic [XLEN-1:0] mtvec;
  logic [XLEN-1:0] mscratch;
  logic [XLEN-1:0] mepc;
  logic [XLEN-1:0] mcause;
  logic [XLEN-1:0] mtval;
  logic [XLEN-1:0] satp;
  logic [XLEN-1:0] mcounteren;
  logic [XLEN-1:0] warp_scratch[0:NUM_WARPS-1];

  // Local event counters make invalid accesses observable through CSRs.
  logic [63:0] read_count;
  logic [63:0] write_count;
  logic [63:0] error_count;

  logic [XLEN-1:0] decode_data;
  logic decode_error;
  logic decode_read_only;
  logic write_address_valid;
  logic [XLEN-1:0] read_data_r;
  logic read_error_r;
  logic read_valid_r;
  logic read_valid_delay_r;

  integer warp_index;

  initial begin : parameter_checks
    if (XLEN != 32 && XLEN != 64) $fatal(1, "XLEN must be 32 or 64");
    if (ADDR_WIDTH < 12) $fatal(1, "ADDR_WIDTH must be at least 12");
    if (NUM_WARPS < 2) $fatal(1, "NUM_WARPS must be at least two");
    if (NUM_THREADS < 1) $fatal(1, "NUM_THREADS must be positive");
    if ((NUM_WARPS & (NUM_WARPS - 1)) != 0) $fatal(1, "NUM_WARPS must be a power of two");
  end

  function automatic [XLEN-1:0] fit64;
    input [63:0] value;
    begin
      if (XLEN == 64) fit64 = value;
      else fit64 = value[31:0];
    end
  endfunction

  function automatic [XLEN-1:0] high64;
    input [63:0] value;
    begin
      if (XLEN == 64) high64 = '0;
      else high64 = value[63:32];
    end
  endfunction

  function automatic [XLEN-1:0] machine_isa;
    logic [XLEN-1:0] value;
    begin
      value = '0;
      value[8] = 1'b1;  // I extension
      value[12] = 1'b1;  // M extension
      if (XLEN == 32) value[31:30] = 2'b01;
      else value[63:62] = 2'b10;
      machine_isa = value;
    end
  endfunction

  // Combinational CSR decoder.  Every implemented address explicitly sets
  // its class so that unknown addresses produce a deterministic zero/error.
  always @(*) begin : read_decode
    decode_data      = '0;
    decode_error     = 1'b0;
    decode_read_only = 1'b0;
    case (read_addr)
      CSR_MVENDORID: begin
        decode_data = XLEN'(VENDOR_ID);
        decode_read_only = 1;
      end
      CSR_MARCHID: begin
        decode_data = XLEN'(ARCH_ID);
        decode_read_only = 1;
      end
      CSR_MIMPID: begin
        decode_data = XLEN'(IMPL_ID);
        decode_read_only = 1;
      end
      CSR_MHARTID: begin
        decode_data = XLEN'(CORE_ID);
        decode_read_only = 1;
      end
      CSR_MCONFIGPTR: begin
        decode_data = '0;
        decode_read_only = 1;
      end
      CSR_MSTATUS:      decode_data = mstatus;
      CSR_MISA: begin
        decode_data = machine_isa();
        decode_read_only = 1;
      end
      CSR_MEDELEG: begin
        decode_data = '0;
        decode_read_only = 1;
      end
      CSR_MIDELEG: begin
        decode_data = '0;
        decode_read_only = 1;
      end
      CSR_MIE:          decode_data = mie;
      CSR_MTVEC:        decode_data = mtvec;
      CSR_MCOUNTEREN:   decode_data = mcounteren;
      CSR_MSCRATCH:     decode_data = mscratch;
      CSR_MEPC:         decode_data = mepc;
      CSR_MCAUSE:       decode_data = mcause;
      CSR_MTVAL:        decode_data = mtval;
      CSR_MIP: begin
        decode_data = '0;
        decode_read_only = 1;
      end
      CSR_SATP:         decode_data = satp;
      CSR_MCYCLE: begin
        decode_data = fit64(cycles);
        decode_read_only = 1;
      end
      CSR_MCYCLEH: begin
        decode_data = high64(cycles);
        decode_read_only = 1;
      end
      CSR_MINSTRET: begin
        decode_data = fit64(instret);
        decode_read_only = 1;
      end
      CSR_MINSTRETH: begin
        decode_data = high64(instret);
        decode_read_only = 1;
      end
      CSR_WARP_ID: begin
        decode_data = XLEN'(read_wid);
        decode_read_only = 1;
      end
      CSR_CORE_ID: begin
        decode_data = XLEN'(CORE_ID);
        decode_read_only = 1;
      end
      CSR_ACTIVE_THREADS: begin
        decode_data = XLEN'(thread_mask);
        decode_read_only = 1;
      end
      CSR_ACTIVE_WARPS: begin
        decode_data = XLEN'(active_warps);
        decode_read_only = 1;
      end
      CSR_NUM_THREADS: begin
        decode_data = XLEN'(NUM_THREADS);
        decode_read_only = 1;
      end
      CSR_NUM_WARPS: begin
        decode_data = XLEN'(NUM_WARPS);
        decode_read_only = 1;
      end
      CSR_NUM_CORES: begin
        decode_data = XLEN'(NUM_CORES * NUM_CLUSTERS);
        decode_read_only = 1;
      end
      CSR_LOCAL_MEM_BASE: begin
        decode_data = XLEN'(LMEM_BASE);
        decode_read_only = 1;
      end
      CSR_READ_COUNT: begin
        decode_data = fit64(read_count);
        decode_read_only = 1;
      end
      CSR_WRITE_COUNT: begin
        decode_data = fit64(write_count);
        decode_read_only = 1;
      end
      CSR_ERROR_COUNT: begin
        decode_data = fit64(error_count);
        decode_read_only = 1;
      end
      CSR_WARP_SCRATCH: decode_data = warp_scratch[read_wid];
      default: begin
        decode_data  = '0;
        decode_error = 1'b1;
      end
    endcase
  end

  always @(*) begin : write_decode
    write_address_valid = 1'b1;
    case (write_addr)
      CSR_MSTATUS,
            CSR_MIE,
            CSR_MTVEC,
            CSR_MCOUNTEREN,
            CSR_MSCRATCH,
            CSR_MEPC,
            CSR_MCAUSE,
            CSR_MTVAL,
            CSR_SATP,
            CSR_WARP_SCRATCH:
      write_address_valid = 1'b1;
      default: write_address_valid = 1'b0;
    endcase
  end

  always @(posedge clk) begin : state_update
    if (reset) begin
      mstatus            <= '0;
      mie                <= '0;
      mtvec              <= '0;
      mscratch           <= XLEN'(STARTUP_ARG);
      mepc               <= '0;
      mcause             <= '0;
      mtval              <= '0;
      satp               <= '0;
      mcounteren         <= '0;
      read_count         <= '0;
      write_count        <= '0;
      error_count        <= '0;
      read_data_r        <= '0;
      read_error_r       <= 1'b0;
      read_valid_r       <= 1'b0;
      read_valid_delay_r <= 1'b0;
      for (warp_index = 0; warp_index < NUM_WARPS; warp_index = warp_index + 1)
      warp_scratch[warp_index] <= '0;
    end else begin
      read_valid_r       <= read_enable;
      read_valid_delay_r <= read_valid_r;
      if (read_enable) begin
        read_data_r  <= decode_data;
        read_error_r <= decode_error;
        read_count   <= read_count + 1'b1;
        if (decode_error) error_count <= error_count + 1'b1;
      end
      if (write_enable) begin
        write_count <= write_count + 1'b1;
        if (!write_address_valid) error_count <= error_count + 1'b1;
        case (write_addr)
          CSR_MSTATUS:      mstatus <= write_data;
          CSR_MIE:          mie <= write_data;
          CSR_MTVEC:        mtvec <= {write_data[XLEN-1:2], 2'b00};
          CSR_MCOUNTEREN:   mcounteren <= write_data;
          CSR_MSCRATCH:     mscratch <= write_data;
          CSR_MEPC:         mepc <= {write_data[XLEN-1:1], 1'b0};
          CSR_MCAUSE:       mcause <= write_data;
          CSR_MTVAL:        mtval <= write_data;
          CSR_SATP:         satp <= write_data;
          CSR_WARP_SCRATCH: warp_scratch[read_wid] <= write_data;
          default: begin
          end
        endcase
      end
    end
  end

  // The mutation file changes the default to one.  Data remains at its
  // architected one-cycle position while valid is moved one cycle later.
  assign read_valid  = TIMING_BUG ? read_valid_delay_r : read_valid_r;
  assign read_data   = read_data_r;
  assign read_error  = read_error_r;
  assign write_ready = 1'b1;

  wire unused_decode_read_only = decode_read_only;
  wire unused_write_address_valid = write_address_valid;
endmodule

`default_nettype wire
