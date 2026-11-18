// Copyright (c) 2019-2023 Vortex project contributors
// Standalone configurable true dual-access RAM for Spec2RTL.
`timescale 1ns / 1ps
`default_nettype none

module VX_dp_ram #(
    parameter integer             DATAW        = 32,
    parameter integer             SIZE         = 64,
    parameter integer             BYTEENW      = DATAW / 8,
    parameter integer             ADDRW        = (SIZE > 1) ? $clog2(SIZE) : 1,
    parameter integer             READ_LATENCY = 1,
    parameter integer             RADDR_REG    = 0,
    parameter integer             OUT_REG      = 0,
    parameter integer             RESET_RAM    = 0,
    parameter integer             INIT_ENABLE  = 0,
    parameter logic   [DATAW-1:0] INIT_VALUE   = '0,
    parameter string              INIT_FILE    = "",
    parameter string              RDW_MODE     = "R",
    parameter integer             TIMING_BUG   = 0
) (
    input  wire               clk,
    input  wire               reset,
    input  wire               write,
    input  wire [BYTEENW-1:0] wren,
    input  wire [  ADDRW-1:0] waddr,
    input  wire [  DATAW-1:0] wdata,
    input  wire               read,
    input  wire [  ADDRW-1:0] raddr,
    output wire               rvalid,
    output wire [  DATAW-1:0] rdata
);
  localparam integer BYTE_WIDTH = DATAW / BYTEENW;
  localparam integer PIPE_DEPTH = READ_LATENCY + OUT_REG + 1;

  logic [DATAW-1:0] ram[0:SIZE-1];
  logic [ADDRW-1:0] read_addr_r;
  logic [DATAW-1:0] memory_read_data;
  logic [DATAW-1:0] collision_data;
  logic [DATAW-1:0] read_data_mux;
  logic [DATAW-1:0] data_pipe[0:PIPE_DEPTH-1];
  logic [PIPE_DEPTH-1:0] valid_pipe;
  logic [DATAW-1:0] hold_data_r;
  logic hold_valid_r;
  logic collision;
  logic read_address_valid;
  logic write_address_valid;
  integer init_index;
  integer byte_index;
  integer pipe_index;

  initial begin : validate_parameters
    if (DATAW < 8) $fatal(1, "DATAW must be at least one byte");
    if (SIZE < 1) $fatal(1, "SIZE must be positive");
    if (BYTEENW < 1) $fatal(1, "BYTEENW must be positive");
    if ((DATAW % BYTEENW) != 0) $fatal(1, "DATAW must be divisible by BYTEENW");
    if ((BYTE_WIDTH % 8) != 0) $fatal(1, "each write-enable slice must contain whole bytes");
    if (READ_LATENCY < 1) $fatal(1, "READ_LATENCY must be at least one");
    if (RADDR_REG != 0 && RADDR_REG != 1) $fatal(1, "RADDR_REG must be zero or one");
    if (OUT_REG != 0 && OUT_REG != 1) $fatal(1, "OUT_REG must be zero or one");
    if (RDW_MODE != "R" && RDW_MODE != "W" && RDW_MODE != "N")
      $fatal(1, "RDW_MODE must be R, W, or N");
  end

  // Optional initialization is deliberately contained in this file.  An
  // initialization file is used only when its name is nonempty; otherwise
  // every word receives INIT_VALUE.
  generate
    if (INIT_ENABLE != 0) begin : g_initialize
      initial begin
        if (INIT_FILE != "") begin
          $readmemh(INIT_FILE, ram);
        end else begin
          for (init_index = 0; init_index < SIZE; init_index = init_index + 1)
          ram[init_index] = INIT_VALUE;
        end
      end
    end
  endgenerate

  always @(*) begin : address_checks
    read_address_valid = (raddr < SIZE);
    write_address_valid = (waddr < SIZE);
    collision = read && write && read_address_valid && write_address_valid && (raddr == waddr);
  end

  // Form the value that would exist after all enabled write slices update.
  // It is also the write-first collision result.
  always @(*) begin : merge_write_data
    if (write_address_valid) collision_data = ram[waddr];
    else collision_data = '0;
    for (byte_index = 0; byte_index < BYTEENW; byte_index = byte_index + 1) begin
      if (wren[byte_index]) begin
        collision_data[byte_index*BYTE_WIDTH +: BYTE_WIDTH] =
                    wdata[byte_index*BYTE_WIDTH +: BYTE_WIDTH];
      end
    end
  end

  // Address-register support matches the useful Vortex RAM configurations.
  generate
    if (RADDR_REG != 0) begin : g_registered_read_address
      always @(posedge clk) begin
        if (reset) read_addr_r <= '0;
        else if (read) read_addr_r <= raddr;
      end

      always @(*) begin
        if (read_addr_r < SIZE) memory_read_data = ram[read_addr_r];
        else memory_read_data = '0;
      end
    end else begin : g_direct_read_address
      always @(*) begin
        read_addr_r = raddr;
        if (read_address_valid) memory_read_data = ram[raddr];
        else memory_read_data = '0;
      end
    end
  endgenerate

  // Select read-during-write semantics. R returns the old RAM value, W
  // forwards enabled write slices, and N preserves the last read value.
  always @(*) begin : select_collision_behavior
    read_data_mux = memory_read_data;
    if (collision) begin
      case (RDW_MODE)
        "W": read_data_mux = collision_data;
        "N": read_data_mux = hold_data_r;
        default: read_data_mux = memory_read_data;
      endcase
    end
  end

  // Four generate branches preserve synthesizer-friendly inference styles.
  // RESET_RAM controls whether RAM contents participate in synchronous reset.
  generate
    if (RESET_RAM != 0 && BYTEENW == 1) begin : g_reset_full_write
      always @(posedge clk) begin
        if (reset) begin
          for (init_index = 0; init_index < SIZE; init_index = init_index + 1)
          ram[init_index] <= INIT_VALUE;
        end else if (write && write_address_valid && wren[0]) begin
          ram[waddr] <= wdata;
        end
      end
    end else if (RESET_RAM != 0 && BYTEENW > 1) begin : g_reset_sliced_write
      always @(posedge clk) begin
        if (reset) begin
          for (init_index = 0; init_index < SIZE; init_index = init_index + 1)
          ram[init_index] <= INIT_VALUE;
        end else if (write && write_address_valid) begin
          for (byte_index = 0; byte_index < BYTEENW; byte_index = byte_index + 1) begin
            if (wren[byte_index])
              ram[waddr][byte_index*BYTE_WIDTH +: BYTE_WIDTH] <=
                                wdata[byte_index*BYTE_WIDTH +: BYTE_WIDTH];
          end
        end
      end
    end else if (RESET_RAM == 0 && BYTEENW == 1) begin : g_noreset_full_write
      always @(posedge clk) begin
        if (write && write_address_valid && wren[0]) ram[waddr] <= wdata;
      end
    end else begin : g_noreset_sliced_write
      always @(posedge clk) begin
        if (write && write_address_valid) begin
          for (byte_index = 0; byte_index < BYTEENW; byte_index = byte_index + 1) begin
            if (wren[byte_index]) begin
              ram[waddr][byte_index*BYTE_WIDTH +: BYTE_WIDTH] <=
                                wdata[byte_index*BYTE_WIDTH +: BYTE_WIDTH];
            end
          end
        end
      end
    end
  endgenerate

  // The first stage samples RAM on a read clock edge. Additional stages are
  // genuine latency stages and can be used to model embedded memories.
  always @(posedge clk) begin : read_pipeline
    if (reset) begin
      valid_pipe   <= '0;
      hold_data_r  <= '0;
      hold_valid_r <= 1'b0;
      for (pipe_index = 0; pipe_index < PIPE_DEPTH; pipe_index = pipe_index + 1)
      data_pipe[pipe_index] <= '0;
    end else begin
      valid_pipe[0] <= read;
      if (read) begin
        data_pipe[0] <= read_data_mux;
        hold_data_r  <= read_data_mux;
        hold_valid_r <= 1'b1;
      end
      for (pipe_index = 1; pipe_index < PIPE_DEPTH; pipe_index = pipe_index + 1) begin
        valid_pipe[pipe_index] <= valid_pipe[pipe_index-1];
        if (valid_pipe[pipe_index-1]) data_pipe[pipe_index] <= data_pipe[pipe_index-1];
      end
    end
  end

  localparam integer DATA_STAGE = READ_LATENCY - 1 + OUT_REG;
  localparam integer VALID_STAGE = DATA_STAGE + TIMING_BUG;

  // The mutation delays only rvalid, so consecutive reads associate valid
  // with the wrong data beat. The reference keeps both at DATA_STAGE.
  assign rvalid = valid_pipe[VALID_STAGE];
  assign rdata  = data_pipe[DATA_STAGE];

`ifndef SYNTHESIS
  // An independent shadow model checks the configurable byte-write behavior.
  // It is simulation-only and therefore does not affect inferred memory style.
  logic [DATAW-1:0] shadow_ram[0:SIZE-1];
  logic [DATAW-1:0] expected_pipe[0:PIPE_DEPTH-1];
  logic [PIPE_DEPTH-1:0] expected_valid_pipe;
  logic [DATAW-1:0] shadow_read_value;
  logic [DATAW-1:0] shadow_write_value;
  integer shadow_index;
  integer shadow_byte;
  integer shadow_stage;

  initial begin : initialize_shadow
    for (shadow_index = 0; shadow_index < SIZE; shadow_index = shadow_index + 1) begin
      shadow_ram[shadow_index] = INIT_ENABLE ? INIT_VALUE : 'x;
    end
  end

  always @(*) begin : calculate_shadow_values
    if (RADDR_REG != 0)
      shadow_read_value = (read_addr_r < SIZE) ? shadow_ram[read_addr_r] : '0;
    else
      shadow_read_value = read_address_valid ? shadow_ram[raddr] : '0;
    shadow_write_value = write_address_valid ? shadow_ram[waddr] : '0;
    for (shadow_byte = 0; shadow_byte < BYTEENW; shadow_byte = shadow_byte + 1) begin
      if (wren[shadow_byte]) begin
        shadow_write_value[shadow_byte*BYTE_WIDTH+:BYTE_WIDTH] =
            wdata[shadow_byte*BYTE_WIDTH+:BYTE_WIDTH];
      end
    end
    if (collision) begin
      case (RDW_MODE)
        "W": shadow_read_value = shadow_write_value;
        "N": shadow_read_value = hold_data_r;
        default: shadow_read_value = shadow_ram[raddr];
      endcase
    end
  end

  always @(posedge clk) begin : update_shadow_model
    if (reset) begin
      expected_valid_pipe <= '0;
      for (shadow_stage = 0; shadow_stage < PIPE_DEPTH; shadow_stage = shadow_stage + 1) begin
        expected_pipe[shadow_stage] <= '0;
      end
      if (RESET_RAM != 0) begin
        for (shadow_index = 0; shadow_index < SIZE; shadow_index = shadow_index + 1) begin
          shadow_ram[shadow_index] <= INIT_VALUE;
        end
      end
    end else begin
      expected_valid_pipe[0] <= read;
      if (read) expected_pipe[0] <= shadow_read_value;
      for (shadow_stage = 1; shadow_stage < PIPE_DEPTH; shadow_stage = shadow_stage + 1) begin
        expected_valid_pipe[shadow_stage] <= expected_valid_pipe[shadow_stage-1];
        if (expected_valid_pipe[shadow_stage-1]) begin
          expected_pipe[shadow_stage] <= expected_pipe[shadow_stage-1];
        end
      end
      if (write && write_address_valid) shadow_ram[waddr] <= shadow_write_value;
      // The generic shadow pipeline models the direct-address configuration.
      // RADDR_REG uses the prior registered address and is checked externally
      // by the specification-driven testbench instead.
      if (RADDR_REG == 0 && rvalid && !$isunknown(expected_pipe[DATA_STAGE])) begin
        assert (rdata === expected_pipe[DATA_STAGE])
        else
          $error(
              "VX_dp_ram response mismatch: got=%h expected=%h", rdata, expected_pipe[DATA_STAGE]
          );
      end
    end
  end

  always @(posedge clk) begin : simulation_protocol_checks
    if (!reset && read && !read_address_valid)
      $error("VX_dp_ram read address %0d is outside SIZE=%0d", raddr, SIZE);
    if (!reset && write && !write_address_valid)
      $error("VX_dp_ram write address %0d is outside SIZE=%0d", waddr, SIZE);
    if (!reset && write && !(|wren)) $warning("VX_dp_ram write asserted with no enabled slices");
    if (!reset && collision && RDW_MODE == "N" && !hold_valid_r)
      $warning("no-change collision occurred before a prior read");
  end
`endif

  wire unused_read_addr = &{1'b0, read_addr_r};
endmodule

`default_nettype wire
