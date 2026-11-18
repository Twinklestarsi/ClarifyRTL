// Copyright (c) 2019-2023
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
// http://www.apache.org/licenses/LICENSE-2.0
//
// Standalone adaptation of the Vortex VX_mem_coalescer.
// This file intentionally has no include files, packages, interfaces, vendor
// primitives, or external module instances.

`timescale 1ns/1ps

module VX_mem_coalescer #(
    // The defaults form a legal, directly elaboratable configuration:
    // sixteen 32-bit lanes coalesce into one 512-bit memory request.
    parameter integer NUM_REQS       = 16,
    parameter integer ADDR_WIDTH     = 32,
    parameter integer FLAGS_WIDTH    = 0,
    parameter integer DATA_IN_SIZE   = 4,
    parameter integer DATA_OUT_SIZE  = 64,
    parameter integer TAG_WIDTH      = 8,
    parameter integer UUID_WIDTH     = 0,
    parameter integer QUEUE_SIZE     = 8,
    parameter integer PERF_CTR_BITS  = (NUM_REQS > 0) ? $clog2(NUM_REQS + 1) : 1,

    parameter integer DATA_IN_WIDTH  = DATA_IN_SIZE * 8,
    parameter integer DATA_OUT_WIDTH = DATA_OUT_SIZE * 8,
    parameter integer DATA_RATIO     = DATA_OUT_SIZE / DATA_IN_SIZE,
    parameter integer DATA_RATIO_W   = (DATA_RATIO > 1) ? $clog2(DATA_RATIO) : 1,
    parameter integer OUT_REQS       = NUM_REQS / DATA_RATIO,
    parameter integer OUT_ADDR_WIDTH = ADDR_WIDTH - DATA_RATIO_W,
    parameter integer QUEUE_ADDRW    = (QUEUE_SIZE > 1) ? $clog2(QUEUE_SIZE) : 1,
    parameter integer OUT_TAG_WIDTH  = UUID_WIDTH + QUEUE_ADDRW,
    parameter integer FLAGS_W        = (FLAGS_WIDTH > 0) ? FLAGS_WIDTH : 1
) (
    input  wire clk,
    input  wire reset,

    output wire [PERF_CTR_BITS-1:0] misses,

    // Narrow, lane-oriented request input.
    input  wire                               in_req_valid,
    input  wire                               in_req_rw,
    input  wire [NUM_REQS-1:0]                in_req_mask,
    input  wire [NUM_REQS-1:0][DATA_IN_SIZE-1:0] in_req_byteen,
    input  wire [NUM_REQS-1:0][ADDR_WIDTH-1:0] in_req_addr,
    input  wire [NUM_REQS-1:0][FLAGS_W-1:0]    in_req_flags,
    input  wire [NUM_REQS-1:0][DATA_IN_WIDTH-1:0] in_req_data,
    input  wire [TAG_WIDTH-1:0]                in_req_tag,
    output wire                                in_req_ready,

    // Narrow response reconstructed for the input lanes.
    output wire                                in_rsp_valid,
    output wire [NUM_REQS-1:0]                 in_rsp_mask,
    output wire [NUM_REQS-1:0][DATA_IN_WIDTH-1:0] in_rsp_data,
    output wire [TAG_WIDTH-1:0]                in_rsp_tag,
    input  wire                                in_rsp_ready,

    // Coalesced wide request output.
    output wire                                out_req_valid,
    output wire                                out_req_rw,
    output wire [OUT_REQS-1:0]                 out_req_mask,
    output wire [OUT_REQS-1:0][DATA_OUT_SIZE-1:0] out_req_byteen,
    output wire [OUT_REQS-1:0][OUT_ADDR_WIDTH-1:0] out_req_addr,
    output wire [OUT_REQS-1:0][FLAGS_W-1:0]     out_req_flags,
    output wire [OUT_REQS-1:0][DATA_OUT_WIDTH-1:0] out_req_data,
    output wire [OUT_TAG_WIDTH-1:0]             out_req_tag,
    input  wire                                 out_req_ready,

    // Wide response input.
    input  wire                                 out_rsp_valid,
    input  wire [OUT_REQS-1:0]                  out_rsp_mask,
    input  wire [OUT_REQS-1:0][DATA_OUT_WIDTH-1:0] out_rsp_data,
    input  wire [OUT_TAG_WIDTH-1:0]             out_rsp_tag,
    output wire                                 out_rsp_ready
);

    localparam integer TAG_ID_WIDTH = TAG_WIDTH - UUID_WIDTH;
    localparam integer IBUF_DATA_WIDTH =
        TAG_ID_WIDTH + NUM_REQS + (NUM_REQS * DATA_RATIO_W);

    localparam logic STATE_WAIT = 1'b0;
    localparam logic STATE_SEND = 1'b1;

    // Elaboration-time checks document the supported parameter space.
    initial begin : g_parameter_checks
        if (NUM_REQS <= 1)
            $fatal(1, "NUM_REQS must be greater than one");
        if (DATA_IN_SIZE <= 0 || DATA_OUT_SIZE <= 0)
            $fatal(1, "data sizes must be positive");
        if ((DATA_OUT_SIZE % DATA_IN_SIZE) != 0)
            $fatal(1, "DATA_OUT_SIZE must be divisible by DATA_IN_SIZE");
        if (DATA_RATIO <= 0 || ((DATA_RATIO & (DATA_RATIO - 1)) != 0))
            $fatal(1, "DATA_RATIO must be a positive power of two");
        if ((NUM_REQS % DATA_RATIO) != 0)
            $fatal(1, "NUM_REQS must be divisible by DATA_RATIO");
        if (ADDR_WIDTH <= DATA_RATIO_W)
            $fatal(1, "ADDR_WIDTH is too small for DATA_RATIO");
        if (QUEUE_SIZE <= 0)
            $fatal(1, "QUEUE_SIZE must be positive");
        if (TAG_WIDTH <= UUID_WIDTH)
            $fatal(1, "TAG_WIDTH must be greater than UUID_WIDTH");
    end

    logic state_r, state_n;

    logic out_req_valid_r, out_req_valid_n;
    logic out_req_rw_r, out_req_rw_n;
    logic [OUT_REQS-1:0] out_req_mask_r, out_req_mask_n;
    logic [OUT_REQS-1:0][OUT_ADDR_WIDTH-1:0] out_req_addr_r, out_req_addr_n;
    logic [OUT_REQS-1:0][FLAGS_W-1:0] out_req_flags_r, out_req_flags_n;
    logic [OUT_REQS-1:0][DATA_RATIO-1:0][DATA_IN_SIZE-1:0]
        out_req_byteen_r, out_req_byteen_n;
    logic [OUT_REQS-1:0][DATA_RATIO-1:0][DATA_IN_WIDTH-1:0]
        out_req_data_r, out_req_data_n;
    logic [OUT_TAG_WIDTH-1:0] out_req_tag_r, out_req_tag_n;

    logic in_req_ready_n;

    wire                       ibuf_push;
    wire                       ibuf_pop;
    wire [QUEUE_ADDRW-1:0]     ibuf_waddr;
    wire [QUEUE_ADDRW-1:0]     ibuf_raddr;
    logic                      ibuf_full;
    wire [IBUF_DATA_WIDTH-1:0] ibuf_din;
    wire [IBUF_DATA_WIDTH-1:0] ibuf_dout;

    logic [OUT_REQS-1:0] batch_valid_r, batch_valid_n;
    logic [OUT_REQS-1:0][OUT_ADDR_WIDTH-1:0] seed_addr_r, seed_addr_n;
    logic [OUT_REQS-1:0][FLAGS_W-1:0] seed_flags_r, seed_flags_n;
    logic [NUM_REQS-1:0] addr_matches_r, addr_matches_n;
    logic [NUM_REQS-1:0] req_rem_mask_r, req_rem_mask_n;

    wire [NUM_REQS-1:0][DATA_RATIO_W-1:0] in_addr_offset;
    wire [NUM_REQS*DATA_RATIO_W-1:0] in_addr_offset_flat = in_addr_offset;
    wire [NUM_REQS*DATA_IN_SIZE-1:0] in_req_byteen_flat = in_req_byteen;
    wire [NUM_REQS*DATA_IN_WIDTH-1:0] in_req_data_flat = in_req_data;
    for (genvar i = 0; i < NUM_REQS; ++i) begin : g_in_addr_offset
        assign in_addr_offset[i] = in_req_addr[i][DATA_RATIO_W-1:0];
    end

    // Select the lowest-numbered remaining lane in each output group.  This
    // replaces the original external VX_priority_encoder instance.
    for (genvar i = 0; i < OUT_REQS; ++i) begin : g_seed_gen
        wire [DATA_RATIO-1:0] batch_mask;
        logic [DATA_RATIO_W-1:0] batch_idx;

        assign batch_mask = in_req_mask[i * DATA_RATIO +: DATA_RATIO]
                          & req_rem_mask_r[i * DATA_RATIO +: DATA_RATIO];
        assign batch_valid_n[i] = |batch_mask;

        always @(*) begin
            batch_idx = '0;
            for (integer j = DATA_RATIO - 1; j >= 0; --j) begin
                if (batch_mask[j])
                    batch_idx = DATA_RATIO_W'(j);
            end
        end

        wire [DATA_RATIO-1:0][OUT_ADDR_WIDTH-1:0] addr_base;
        wire [DATA_RATIO-1:0][FLAGS_W-1:0] req_flags;
        for (genvar j = 0; j < DATA_RATIO; ++j) begin : g_group_inputs
            assign addr_base[j] =
                in_req_addr[DATA_RATIO * i + j][ADDR_WIDTH-1:DATA_RATIO_W];
            assign req_flags[j] = in_req_flags[DATA_RATIO * i + j];
            assign addr_matches_n[i * DATA_RATIO + j] =
                (addr_base[j] == seed_addr_n[i]);
        end

        assign seed_addr_n[i]  = addr_base[batch_idx];
        assign seed_flags_n[i] = req_flags[batch_idx];
    end

    wire [NUM_REQS-1:0] current_pmask = in_req_mask & addr_matches_r;

    wire [OUT_REQS-1:0][DATA_RATIO-1:0][DATA_IN_SIZE-1:0]
        req_byteen_merged;
    wire [OUT_REQS-1:0][DATA_RATIO-1:0][DATA_IN_WIDTH-1:0]
        req_data_merged;

    // Merge all enabled input bytes that target the selected wide word.
    for (genvar i = 0; i < OUT_REQS; ++i) begin : g_data_merged
        // Flat vectors are used internally for compatibility with simulators
        // that do not support variable indexing into packed dimensions.
        logic [DATA_OUT_SIZE-1:0] byteen_merged;
        logic [DATA_OUT_WIDTH-1:0] data_merged;
        always @(*) begin
            byteen_merged = '0;
            data_merged   = '0;
            for (integer j = 0; j < DATA_RATIO; ++j) begin
                for (integer k = 0; k < DATA_IN_SIZE; ++k) begin
                    if (current_pmask[i * DATA_RATIO + j]
                     && in_req_byteen_flat[
                            (DATA_RATIO * i + j) * DATA_IN_SIZE + k
                        ]) begin
                        byteen_merged[
                            in_addr_offset_flat[
                                (DATA_RATIO * i + j) * DATA_RATIO_W
                                +: DATA_RATIO_W
                            ] * DATA_IN_SIZE + k
                        ] = 1'b1;
                        data_merged[
                            (in_addr_offset_flat[
                                (DATA_RATIO * i + j) * DATA_RATIO_W
                                +: DATA_RATIO_W
                            ] * DATA_IN_SIZE + k) * 8
                            +: 8
                        ]
                            = in_req_data_flat[
                                (DATA_RATIO * i + j) * DATA_IN_WIDTH + k*8
                                +: 8
                            ];
                    end
                end
            end
        end
        assign req_byteen_merged[i] = byteen_merged;
        assign req_data_merged[i]   = data_merged;
    end

    wire is_last_batch =
        ~(|(in_req_mask & ~addr_matches_r & req_rem_mask_r));
    wire out_req_fire = out_req_valid && out_req_ready;

    wire [OUT_TAG_WIDTH-1:0] new_out_req_tag;
    if (UUID_WIDTH > 0) begin : g_new_tag_with_uuid
        assign new_out_req_tag = {
            in_req_tag[TAG_WIDTH-1 -: UUID_WIDTH], ibuf_waddr
        };
    end else begin : g_new_tag_without_uuid
        assign new_out_req_tag = ibuf_waddr;
    end

    always @(*) begin
        state_n          = state_r;
        out_req_valid_n  = out_req_valid_r;
        out_req_mask_n   = out_req_mask_r;
        out_req_rw_n     = out_req_rw_r;
        out_req_addr_n   = out_req_addr_r;
        out_req_flags_n  = out_req_flags_r;
        out_req_byteen_n = out_req_byteen_r;
        out_req_data_n   = out_req_data_r;
        out_req_tag_n    = out_req_tag_r;
        req_rem_mask_n   = req_rem_mask_r;
        in_req_ready_n   = 1'b0;

        case (state_r)
            STATE_WAIT: begin
                if (out_req_fire)
                    out_req_valid_n = 1'b0;
                if (in_req_valid && ~out_req_valid_n && ~ibuf_full)
                    state_n = STATE_SEND;
            end
            default: begin // STATE_SEND
                state_n          = STATE_WAIT;
                out_req_valid_n  = 1'b1;
                out_req_mask_n   = batch_valid_r;
                out_req_rw_n     = in_req_rw;
                out_req_addr_n   = seed_addr_r;
                out_req_flags_n  = seed_flags_r;
                out_req_byteen_n = req_byteen_merged;
                out_req_data_n   = req_data_merged;
                out_req_tag_n    = new_out_req_tag;
                req_rem_mask_n   = is_last_batch
                                 ? {NUM_REQS{1'b1}}
                                 : (req_rem_mask_r & ~current_pmask);
                in_req_ready_n   = is_last_batch;
            end
        endcase
    end

    // The original VX_pipe_register has one stage and resets only state,
    // remaining-mask, and request-valid.  Resetting the payload as well makes
    // the standalone block deterministic without changing valid behavior.
    always @(posedge clk) begin
        if (reset) begin
            state_r          <= STATE_WAIT;
            req_rem_mask_r   <= {NUM_REQS{1'b1}};
            out_req_valid_r  <= 1'b0;
            out_req_rw_r     <= 1'b0;
            addr_matches_r   <= '0;
            batch_valid_r    <= '0;
            out_req_mask_r   <= '0;
            seed_addr_r      <= '0;
            seed_flags_r     <= '0;
            out_req_addr_r   <= '0;
            out_req_flags_r  <= '0;
            out_req_byteen_r <= '0;
            out_req_data_r   <= '0;
            out_req_tag_r    <= '0;
        end else begin
            state_r          <= state_n;
            req_rem_mask_r   <= req_rem_mask_n;
            out_req_valid_r  <= out_req_valid_n;
            out_req_rw_r     <= out_req_rw_n;
            addr_matches_r   <= addr_matches_n;
            batch_valid_r    <= batch_valid_n;
            out_req_mask_r   <= out_req_mask_n;
            seed_addr_r      <= seed_addr_n;
            seed_flags_r     <= seed_flags_n;
            out_req_addr_r   <= out_req_addr_n;
            out_req_flags_r  <= out_req_flags_n;
            out_req_byteen_r <= out_req_byteen_n;
            out_req_data_r   <= out_req_data_n;
            out_req_tag_r    <= out_req_tag_n;
        end
    end

    wire out_rsp_fire = out_rsp_valid && out_rsp_ready;
    wire req_sent     = (state_r == STATE_SEND);
    wire out_rsp_eop;

    assign ibuf_push  = req_sent && ~in_req_rw;
    assign ibuf_pop   = out_rsp_fire && out_rsp_eop;
    assign ibuf_raddr = out_rsp_tag[QUEUE_ADDRW-1:0];
    assign ibuf_waddr = alloc_addr_r;

    wire [TAG_ID_WIDTH-1:0] ibuf_din_tag =
        in_req_tag[TAG_ID_WIDTH-1:0];
    wire [NUM_REQS-1:0][DATA_RATIO_W-1:0] ibuf_din_offset =
        in_addr_offset;
    wire [NUM_REQS-1:0] ibuf_din_pmask = current_pmask;
    assign ibuf_din = {ibuf_din_tag, ibuf_din_pmask, ibuf_din_offset};

    // Standalone index allocator. Released slots become available before the
    // next allocation is selected, matching VX_allocator behavior.
    logic [QUEUE_SIZE-1:0] free_slots, free_slots_n;
    logic [QUEUE_ADDRW-1:0] alloc_addr_r;
    logic [QUEUE_ADDRW-1:0] free_index;
    logic free_valid;

    always @(*) begin
        free_slots_n = free_slots;
        if (ibuf_pop)
            free_slots_n[ibuf_raddr] = 1'b1;
        if (ibuf_push)
            free_slots_n[alloc_addr_r] = 1'b0;

        free_index = '0;
        free_valid = 1'b0;
        for (integer i = QUEUE_SIZE - 1; i >= 0; --i) begin
            if (free_slots_n[i]) begin
                free_index = QUEUE_ADDRW'(i);
                free_valid = 1'b1;
            end
        end
    end

    always @(posedge clk) begin
        if (reset) begin
            free_slots  <= {QUEUE_SIZE{1'b1}};
            alloc_addr_r <= '0;
            ibuf_full   <= 1'b0;
        end else begin
            free_slots <= free_slots_n;
            ibuf_full  <= ~free_valid;
            if (ibuf_push || (ibuf_pop && ibuf_full))
                alloc_addr_r <= free_index;
        end
    end

    // Asynchronous-read, synchronous-write storage replaces VX_index_buffer
    // and its VX_dp_ram dependency.
    logic [IBUF_DATA_WIDTH-1:0] ibuf_mem [0:QUEUE_SIZE-1];
    always @(posedge clk) begin
        if (ibuf_push)
            ibuf_mem[ibuf_waddr] <= ibuf_din;
    end
    assign ibuf_dout = ibuf_mem[ibuf_raddr];

    assign out_req_valid  = out_req_valid_r;
    assign out_req_rw     = out_req_rw_r;
    assign out_req_mask   = out_req_mask_r;
    assign out_req_byteen = out_req_byteen_r;
    assign out_req_addr   = out_req_addr_r;
    if (FLAGS_WIDTH > 0) begin : g_output_flags
        assign out_req_flags = out_req_flags_r;
    end else begin : g_no_output_flags
        assign out_req_flags = '0;
    end
    assign out_req_data  = out_req_data_r;
    assign out_req_tag   = out_req_tag_r;
    assign in_req_ready  = in_req_ready_n;

    // Track which wide response lanes remain for each outstanding read.
    logic [QUEUE_SIZE-1:0][OUT_REQS-1:0] rsp_rem_mask;
    wire [OUT_REQS-1:0] rsp_rem_mask_n =
        rsp_rem_mask[ibuf_raddr] & ~out_rsp_mask;
    assign out_rsp_eop = ~(|rsp_rem_mask_n);

    always @(posedge clk) begin
        if (reset) begin
            rsp_rem_mask <= '0;
        end else begin
            if (ibuf_push)
                rsp_rem_mask[ibuf_waddr] <= batch_valid_r;
            if (out_rsp_fire)
                rsp_rem_mask[ibuf_raddr] <= rsp_rem_mask_n;
        end
    end

    wire [NUM_REQS-1:0][DATA_RATIO_W-1:0] ibuf_dout_offset;
    wire [NUM_REQS-1:0] ibuf_dout_pmask;
    wire [TAG_ID_WIDTH-1:0] ibuf_dout_tag;
    assign {ibuf_dout_tag, ibuf_dout_pmask, ibuf_dout_offset} = ibuf_dout;

    wire [NUM_REQS-1:0][DATA_IN_WIDTH-1:0] in_rsp_data_n;
    wire [NUM_REQS-1:0] in_rsp_mask_n;
    for (genvar i = 0; i < OUT_REQS; ++i) begin : g_unmerge_groups
        for (genvar j = 0; j < DATA_RATIO; ++j) begin : g_unmerge_lanes
            localparam integer LANE = i * DATA_RATIO + j;
            assign in_rsp_data_n[LANE] =
                out_rsp_data[i][ibuf_dout_offset[LANE] * DATA_IN_WIDTH
                                +: DATA_IN_WIDTH];
            assign in_rsp_mask_n[LANE] =
                out_rsp_mask[i] && ibuf_dout_pmask[LANE];
        end
    end

    assign in_rsp_valid = out_rsp_valid;
    assign in_rsp_mask  = in_rsp_mask_n;
    assign in_rsp_data  = in_rsp_data_n;
    if (UUID_WIDTH > 0) begin : g_response_tag_with_uuid
        assign in_rsp_tag = {
            out_rsp_tag[OUT_TAG_WIDTH-1 -: UUID_WIDTH], ibuf_dout_tag
        };
    end else begin : g_response_tag_without_uuid
        assign in_rsp_tag = ibuf_dout_tag;
    end
    assign out_rsp_ready = in_rsp_ready;

    // Count partial transfers, i.e. additional batches required after the
    // first coalesced request for an input packet.
    logic [PERF_CTR_BITS-1:0] misses_r;
    wire partial_transfer = out_req_fire && (req_rem_mask_r != {NUM_REQS{1'b1}});

    always @(posedge clk) begin
        if (reset)
            misses_r <= '0;
        else
            misses_r <= misses_r + PERF_CTR_BITS'(partial_transfer);
    end
    assign misses = misses_r;

endmodule

