`timescale 1ns / 1ps
`default_nettype none

// Standalone multi-warp register dependency scoreboard.
// Each warp owns a one-entry input stage.  Operand hazards are checked after
// same-cycle writeback bypass, then a fair arbiter feeds a held issue output.
module VX_scoreboard #(
    parameter integer NUM_WARPS = 4,
    parameter integer NUM_REGS = 32,
    parameter integer TAGW = 8,
    parameter integer OPW = 8,
    parameter integer COUNTW = 16,
    parameter integer TIMING_BUG = 0,
    parameter integer REGW = (NUM_REGS > 1) ? $clog2(NUM_REGS) : 1,
    parameter integer WIDW = (NUM_WARPS > 1) ? $clog2(NUM_WARPS) : 1
) (
    input  wire                              clk,
    input  wire                              reset,
    input  wire                              flush,
    input  wire [NUM_WARPS-1:0]              req_valid,
    output logic [NUM_WARPS-1:0]              req_ready,
    input  wire [NUM_WARPS-1:0][2:0]         req_used_rs,
    input  wire [NUM_WARPS-1:0][REGW-1:0]    req_rs1,
    input  wire [NUM_WARPS-1:0][REGW-1:0]    req_rs2,
    input  wire [NUM_WARPS-1:0][REGW-1:0]    req_rs3,
    input  wire [NUM_WARPS-1:0]              req_wb,
    input  wire [NUM_WARPS-1:0][REGW-1:0]    req_rd,
    input  wire [NUM_WARPS-1:0][TAGW-1:0]    req_tag,
    input  wire [NUM_WARPS-1:0][OPW-1:0]     req_op,
    input  wire                              wb_valid,
    input  wire [WIDW-1:0]                   wb_wid,
    input  wire [REGW-1:0]                   wb_rd,
    output wire                              issue_valid,
    input  wire                              issue_ready,
    output wire [WIDW-1:0]                   issue_wid,
    output wire [REGW-1:0]                   issue_rd,
    output wire                              issue_wb,
    output wire [TAGW-1:0]                   issue_tag,
    output wire [OPW-1:0]                    issue_op,
    output wire [NUM_WARPS-1:0][NUM_REGS-1:0] inuse_mask,
    output wire [NUM_WARPS-1:0]              staged_valid,
    output logic [COUNTW-1:0]                 issue_count,
    output logic [COUNTW-1:0]                 hazard_stall_count,
    output logic [NUM_WARPS-1:0][COUNTW-1:0] warp_issue_count
);

    logic [NUM_WARPS-1:0][NUM_REGS-1:0] inuse_r;
    logic [NUM_WARPS-1:0][NUM_REGS-1:0] inuse_after_release;

    logic [NUM_WARPS-1:0] stage_valid_r;
    logic [NUM_WARPS-1:0][2:0] stage_used_rs_r;
    logic [NUM_WARPS-1:0][REGW-1:0] stage_rs1_r;
    logic [NUM_WARPS-1:0][REGW-1:0] stage_rs2_r;
    logic [NUM_WARPS-1:0][REGW-1:0] stage_rs3_r;
    logic [NUM_WARPS-1:0] stage_wb_r;
    logic [NUM_WARPS-1:0][REGW-1:0] stage_rd_r;
    logic [NUM_WARPS-1:0][TAGW-1:0] stage_tag_r;
    logic [NUM_WARPS-1:0][OPW-1:0] stage_op_r;

    logic [NUM_WARPS-1:0] raw_busy1;
    logic [NUM_WARPS-1:0] raw_busy2;
    logic [NUM_WARPS-1:0] raw_busy3;
    logic [NUM_WARPS-1:0] waw_busy;
    logic [NUM_WARPS-1:0] operands_ready;
    logic [NUM_WARPS-1:0] eligible;

    logic [WIDW-1:0] rr_ptr_r;
    logic arb_valid;
    logic [WIDW-1:0] arb_index;
    logic [NUM_WARPS-1:0] arb_onehot;

    logic out_valid_r;
    logic [WIDW-1:0] out_wid_r;
    logic [REGW-1:0] out_rd_r;
    logic out_wb_r;
    logic [TAGW-1:0] out_tag_r;
    logic [OPW-1:0] out_op_r;

    logic delayed_wb_valid_r;
    logic [WIDW-1:0] delayed_wb_wid_r;
    logic [REGW-1:0] delayed_wb_rd_r;
    logic effective_wb_valid;
    logic [WIDW-1:0] effective_wb_wid;
    logic [REGW-1:0] effective_wb_rd;

    logic invalid_writeback_seen_r;
    logic duplicate_reservation_seen_r;
    logic [COUNTW-1:0] output_stall_count_r;
    logic [NUM_WARPS-1:0][COUNTW-1:0] warp_hazard_count_r;

    integer warp;
    integer offset;
    integer scan_index;

    wire output_can_load;
    wire output_fire;
    wire arb_fire;

    initial begin
        if (NUM_WARPS < 1) begin
            $fatal(1, "NUM_WARPS must be positive");
        end
        if (NUM_REGS < 2) begin
            $fatal(1, "NUM_REGS must include register zero");
        end
    end

    // Mutation chooses the registered writeback rather than same-cycle data.
    always_comb begin
        if (TIMING_BUG) begin
            effective_wb_valid = delayed_wb_valid_r;
            effective_wb_wid = delayed_wb_wid_r;
            effective_wb_rd = delayed_wb_rd_r;
        end else begin
            effective_wb_valid = wb_valid;
            effective_wb_wid = wb_wid;
            effective_wb_rd = wb_rd;
        end
    end

    // The release mask is visible to hazard checks in the writeback cycle.
    always_comb begin
        inuse_after_release = inuse_r;
        if (effective_wb_valid && (effective_wb_rd != '0)) begin
            inuse_after_release[effective_wb_wid][effective_wb_rd] = 1'b0;
        end
        for (warp = 0; warp < NUM_WARPS; warp = warp + 1) begin
            inuse_after_release[warp][0] = 1'b0;
        end
    end

    // RAW checks cover all three optional source operands; WAW covers rd.
    always_comb begin
        for (warp = 0; warp < NUM_WARPS; warp = warp + 1) begin
            raw_busy1[warp] = stage_used_rs_r[warp][0]
                              && (stage_rs1_r[warp] != '0)
                              && inuse_after_release[warp][stage_rs1_r[warp]];
            raw_busy2[warp] = stage_used_rs_r[warp][1]
                              && (stage_rs2_r[warp] != '0)
                              && inuse_after_release[warp][stage_rs2_r[warp]];
            raw_busy3[warp] = stage_used_rs_r[warp][2]
                              && (stage_rs3_r[warp] != '0)
                              && inuse_after_release[warp][stage_rs3_r[warp]];
            waw_busy[warp] = stage_wb_r[warp]
                             && (stage_rd_r[warp] != '0)
                             && inuse_after_release[warp][stage_rd_r[warp]];
            operands_ready[warp] = !(raw_busy1[warp]
                                     || raw_busy2[warp]
                                     || raw_busy3[warp]
                                     || waw_busy[warp]);
            eligible[warp] = stage_valid_r[warp] && operands_ready[warp];
        end
    end

    // Fair circular arbitration across warps with ready operands.
    always_comb begin
        arb_valid = 1'b0;
        arb_index = rr_ptr_r;
        for (offset = 0; offset < NUM_WARPS; offset = offset + 1) begin
            scan_index = rr_ptr_r + offset;
            if (scan_index >= NUM_WARPS) begin
                scan_index = scan_index - NUM_WARPS;
            end
            if (!arb_valid && eligible[scan_index]) begin
                arb_valid = 1'b1;
                arb_index = WIDW'(scan_index);
            end
        end
    end

    always_comb begin
        arb_onehot = '0;
        if (arb_valid) begin
            arb_onehot[arb_index] = 1'b1;
        end
    end

    assign output_fire = out_valid_r && issue_ready;
    assign output_can_load = !out_valid_r || output_fire;
    assign arb_fire = arb_valid && output_can_load;

    // A stage can accept a replacement in the cycle its current item issues.
    always_comb begin
        for (warp = 0; warp < NUM_WARPS; warp = warp + 1) begin
            req_ready[warp] = !stage_valid_r[warp]
                              || (arb_fire && arb_onehot[warp]);
        end
    end

    // Per-warp staging decouples fetch/decode from dependency resolution.
    always_ff @(posedge clk) begin
        if (reset || flush) begin
            stage_valid_r <= '0;
            stage_used_rs_r <= '0;
            stage_rs1_r <= '0;
            stage_rs2_r <= '0;
            stage_rs3_r <= '0;
            stage_wb_r <= '0;
            stage_rd_r <= '0;
            stage_tag_r <= '0;
            stage_op_r <= '0;
        end else begin
            for (warp = 0; warp < NUM_WARPS; warp = warp + 1) begin
                if (arb_fire && arb_onehot[warp]) begin
                    stage_valid_r[warp] <= 1'b0;
                end
                if (req_valid[warp] && req_ready[warp]) begin
                    stage_valid_r[warp] <= 1'b1;
                    stage_used_rs_r[warp] <= req_used_rs[warp];
                    stage_rs1_r[warp] <= req_rs1[warp];
                    stage_rs2_r[warp] <= req_rs2[warp];
                    stage_rs3_r[warp] <= req_rs3[warp];
                    stage_wb_r[warp] <= req_wb[warp];
                    stage_rd_r[warp] <= req_rd[warp];
                    stage_tag_r[warp] <= req_tag[warp];
                    stage_op_r[warp] <= req_op[warp];
                end
            end
        end
    end

    // Output payload remains stable while downstream applies backpressure.
    always_ff @(posedge clk) begin
        if (reset || flush) begin
            out_valid_r <= 1'b0;
            out_wid_r <= '0;
            out_rd_r <= '0;
            out_wb_r <= 1'b0;
            out_tag_r <= '0;
            out_op_r <= '0;
        end else begin
            if (output_fire) begin
                out_valid_r <= 1'b0;
            end
            if (arb_fire) begin
                out_valid_r <= 1'b1;
                out_wid_r <= arb_index;
                out_rd_r <= stage_rd_r[arb_index];
                out_wb_r <= stage_wb_r[arb_index];
                out_tag_r <= stage_tag_r[arb_index];
                out_op_r <= stage_op_r[arb_index];
            end
        end
    end

    // Reservations occur when an instruction leaves its staging slot.
    always_ff @(posedge clk) begin
        if (reset || flush) begin
            inuse_r <= '0;
        end else begin
            inuse_r <= inuse_after_release;
            if (arb_fire
                && stage_wb_r[arb_index]
                && (stage_rd_r[arb_index] != '0)) begin
                inuse_r[arb_index][stage_rd_r[arb_index]] <= 1'b1;
            end
        end
    end

    always_ff @(posedge clk) begin
        if (reset) begin
            rr_ptr_r <= '0;
        end else if (arb_fire) begin
            if (arb_index == WIDW'(NUM_WARPS - 1)) begin
                rr_ptr_r <= '0;
            end else begin
                rr_ptr_r <= arb_index + 1'b1;
            end
        end
    end

    always_ff @(posedge clk) begin
        if (reset) begin
            delayed_wb_valid_r <= 1'b0;
            delayed_wb_wid_r <= '0;
            delayed_wb_rd_r <= '0;
        end else begin
            delayed_wb_valid_r <= wb_valid;
            delayed_wb_wid_r <= wb_wid;
            delayed_wb_rd_r <= wb_rd;
        end
    end

    // Global and per-warp counters make stalls and fairness measurable.
    always_ff @(posedge clk) begin
        if (reset) begin
            issue_count <= '0;
            hazard_stall_count <= '0;
            output_stall_count_r <= '0;
            warp_issue_count <= '0;
            warp_hazard_count_r <= '0;
        end else begin
            if (arb_fire) begin
                issue_count <= issue_count + 1'b1;
                warp_issue_count[arb_index]
                    <= warp_issue_count[arb_index] + 1'b1;
            end
            if (|(stage_valid_r & ~operands_ready)) begin
                hazard_stall_count <= hazard_stall_count + 1'b1;
            end
            if (out_valid_r && !issue_ready) begin
                output_stall_count_r <= output_stall_count_r + 1'b1;
            end
            for (warp = 0; warp < NUM_WARPS; warp = warp + 1) begin
                if (stage_valid_r[warp] && !operands_ready[warp]) begin
                    warp_hazard_count_r[warp]
                        <= warp_hazard_count_r[warp] + 1'b1;
                end
            end
        end
    end

    always_ff @(posedge clk) begin
        if (reset) begin
            invalid_writeback_seen_r <= 1'b0;
            duplicate_reservation_seen_r <= 1'b0;
        end else begin
            if (wb_valid
                && (wb_rd != '0)
                && !inuse_r[wb_wid][wb_rd]) begin
                invalid_writeback_seen_r <= 1'b1;
            end
            if (arb_fire
                && stage_wb_r[arb_index]
                && inuse_after_release[arb_index][stage_rd_r[arb_index]]) begin
                duplicate_reservation_seen_r <= 1'b1;
            end
        end
    end

    assign issue_valid = out_valid_r;
    assign issue_wid = out_wid_r;
    assign issue_rd = out_rd_r;
    assign issue_wb = out_wb_r;
    assign issue_tag = out_tag_r;
    assign issue_op = out_op_r;
    assign inuse_mask = inuse_r;
    assign staged_valid = stage_valid_r;

endmodule

`default_nettype wire
